"""Pure-logic tests for hungergames.engine (no discord, no redbot)."""
import random
from datetime import date, datetime

import pytest

from hungergames import content, engine
from hungergames.constants import HOME_TZ, ITEMS, PARACHUTES_PER_DAY

POOL = content.load_events()
CIPHERS = content.load_ciphers()


def make_edition(n, seed, *, items=True):
    rng = random.Random(seed)
    ed = engine.new_edition(1, engine.test_schedule(0), test=True, channel_id=1, rng=rng)
    for i in range(n):
        t = engine.new_tribute(f"T{i}", "drafted")
        t["stats"] = {k: rng.randint(0, 100) for k in ("agility", "strength", "endurance", "intellect", "charisma")}
        t["strategy"] = rng.choice(["hunt", "hide", "forage"])
        if items and rng.random() < 0.3:
            t["inventory"]["consumables"] += rng.sample(["water", "bread", "stew", "serum", "burn_cream", "med_kit"], 2)
        if items and rng.random() < 0.2:
            t["inventory"]["gear"].append(rng.choice(["weapon", "armor", "survival_kit"]))
        ed["roster"][str(i)] = t
    return ed, rng


# ---------------------------------------------------------------- pacing


def test_pacing_over_1000_seeded_runs():
    """Never fewer than 2 alive before Saturday, exactly one victor after the finale, 2-3 into the finale
    almost always, for every roster size from 4 to 12."""
    into_finale = []
    for seed in range(1000):
        ed, rng = make_edition(4 + seed % 9, seed)
        for _ in range(4):
            engine.run_broadcast(ed, POOL, rng)
            assert len(engine.alive_ids(ed)) >= 2, seed
        into_finale.append(len(engine.alive_ids(ed)))
        queue = engine.run_broadcast(ed, POOL, rng)
        assert len(engine.alive_ids(ed)) == 1, seed
        assert ed["victor"] == engine.alive_ids(ed)[0]
        assert queue[-1] == {"t": "victor", "uid": ed["victor"]}
    share = sum(1 for n in into_finale if n in (2, 3)) / len(into_finale)
    assert share >= 0.9


def test_no_early_finish_even_with_a_tiny_roster():
    for seed in range(200):
        ed, rng = make_edition(4, seed)
        for _ in range(4):
            engine.run_broadcast(ed, POOL, rng)
            assert ed["victor"] is None
            assert len(engine.alive_ids(ed)) >= 2


def test_rendered_lines_never_leak_placeholders_or_markers():
    for seed in range(150):
        ed, rng = make_edition(12, seed)
        for _ in range(5):
            for item in engine.run_broadcast(ed, POOL, rng):
                if item["t"] == "line":
                    assert "{" not in item["text"] and "(Elimination" not in item["text"], item["text"]


def test_no_template_repeats_within_an_edition():
    ed, rng = make_edition(12, 7)
    seen = []
    for _ in range(5):
        before = set(ed["used"])
        engine.run_broadcast(ed, POOL, rng)
        seen += sorted(set(ed["used"]) - before)
    assert len(seen) == len(set(seen))


def test_kill_credit_and_deaths_are_consistent():
    for seed in range(100):
        ed, rng = make_edition(10, seed)
        for _ in range(5):
            engine.run_broadcast(ed, POOL, rng)
        dead = [t for t in ed["roster"].values() if not t["alive"]]
        assert len(dead) == 9
        credited = sum(len(t["death"]["by"]) for t in dead)
        assert sum(t["kills"] for t in ed["roster"].values()) == credited


def test_allies_dont_kill_each_other_while_more_than_four_live():
    for seed in range(200):
        ed, rng = make_edition(12, seed, items=False)
        ids = list(ed["roster"])
        ed["alliances"] = [ids[0:3], ids[3:6]]
        engine.run_broadcast(ed, POOL, rng)   # the bloodbath leaves 12 * (65-75%) alive: always > 4
        for t in ed["roster"].values():
            if t["death"] and t["death"]["by"]:
                victim = [u for u, x in ed["roster"].items() if x is t][0]
                group = next((g for g in ([ids[0:3], ids[3:6]]) if victim in g), [])
                assert not set(t["death"]["by"]) & (set(group) - {victim}), seed


# ---------------------------------------------------------------- soft cap


@pytest.mark.parametrize("raw,expected", [(10, 15), (40, 40), (60, 60), (70, 65), (80, 70), (100, 75), (112, 78), (400, 90)])
def test_soft_cap_points(raw, expected):
    assert engine.soft_cap(raw) == pytest.approx(expected)


def test_soft_cap_never_decreases():
    values = [engine.soft_cap(r / 2) for r in range(0, 600)]
    assert all(b >= a for a, b in zip(values, values[1:]))


def test_best_realistic_stack_stays_under_80():
    ed, rng = make_edition(2, 1, items=False)
    t = ed["roster"]["0"]
    t["stats"]["endurance"] = 100
    t["strategy"] = "hunt"
    t["inventory"]["gear"] = ["survival_kit", "armor"]
    t["inventory"]["consumables"] = ["bread"]
    arena = engine.Arena(ed, POOL, rng)
    chance = arena.survival_chance("0", "endurance")
    assert 75 <= chance <= 80
    assert t["inventory"]["consumables"] == []   # bread is used up by the check


# ---------------------------------------------------------------- consumables


def test_matching_consumable_saves_and_is_used_up():
    ed, rng = make_edition(3, 2, items=False)
    ed["roster"]["0"]["inventory"]["consumables"] = ["burn_cream", "med_kit"]
    arena = engine.Arena(ed, POOL, rng)
    assert arena.try_save("0", "fire") is True
    assert ed["roster"]["0"]["inventory"]["consumables"] == ["med_kit"]   # the specific item first, the Med Kit kept


def test_med_kit_saves_about_70_percent():
    saved = 0
    for seed in range(2000):
        ed, rng = make_edition(2, seed, items=False)
        ed["roster"]["0"]["inventory"]["consumables"] = ["med_kit"]
        saved += engine.Arena(ed, POOL, random.Random(seed)).try_save("0", "combat")
    assert 0.65 < saved / 2000 < 0.75


SAVED_MARKERS = ("sponsors come through at the last second", "a silver parachute changes everything")


def test_a_saved_fight_is_narrated_once():
    """A Med Kit save in a fight used to post its own item line AND the generic "sponsors come through"
    line, so the same rescue showed twice (and the item line could be about a different hazard)."""
    saves = 0
    for seed in range(300):
        ed, rng = make_edition(6, seed, items=False)
        for t in ed["roster"].values():
            t["inventory"]["consumables"] = ["med_kit"]
        arena = engine.Arena(ed, POOL, random.Random(seed))
        arena.combat(engine.alive_ids(ed), "day", 2)
        saved = [ln for ln in arena.lines if any(m in ln.get("text", "") for m in SAVED_MARKERS)]
        if saved:
            saves += 1
            assert len(arena.lines) == 1, [ln.get("text") for ln in arena.lines]
    assert saves > 20


def test_a_saved_betrayal_is_narrated_once():
    saves = 0
    for seed in range(600):
        ed, rng = make_edition(6, seed, items=False)
        ed["alliances"] = [["0", "1", "2"]]
        for t in ed["roster"].values():
            t["inventory"]["consumables"] = ["med_kit"]
        arena = engine.Arena(ed, POOL, random.Random(seed))
        arena.betrayal(engine.alive_ids(ed), "day", 1)
        saved = [ln for ln in arena.lines if any(m in ln.get("text", "") for m in SAVED_MARKERS)]
        if saved:
            saves += 1
            assert len(arena.lines) == 1, [ln.get("text") for ln in arena.lines]
    assert saves > 5


def test_unused_consumables_become_a_finale_bonus():
    ed, rng = make_edition(3, 3, items=False)
    for i in range(1, 3):
        ed["roster"][str(i)]["alive"] = i == 1
    ed["roster"]["0"]["inventory"]["consumables"] = ["water", "serum"]
    ed["broadcasts_done"] = 4
    queue = engine.run_broadcast(ed, POOL, rng)
    assert ed["roster"]["0"]["inventory"]["consumables"] == []
    assert any("+20 in the Finale" in q.get("text", "") for q in queue)


# ---------------------------------------------------------------- shop


def _shop_ed():
    ed, _ = make_edition(4, 4, items=False)
    return ed


def test_one_item_per_sponsor_per_tribute_but_any_number_of_tributes():
    ed = _shop_ed()
    assert engine.purchase_error(ed, "99", "0", "water", "d1") is None
    engine.apply_purchase(ed, "99", "0", "water", 1500, "d1", 0)
    assert "already sent" in engine.purchase_error(ed, "99", "0", "bread", "d2")
    for t in ("1", "2", "3"):
        assert engine.purchase_error(ed, "99", t, "bread", "d1") is None
        engine.apply_purchase(ed, "99", t, "bread", 2500, "d1", 0)


def test_three_parachutes_per_tribute_per_day():
    ed = _shop_ed()
    for i in range(PARACHUTES_PER_DAY):
        engine.apply_purchase(ed, str(100 + i), "0", "water", 1500, "d1", 0)
    assert "parachutes today" in engine.purchase_error(ed, "200", "0", "bread", "d1")
    assert engine.purchase_error(ed, "200", "0", "bread", "d2") is None


def test_no_duplicate_gear_no_self_sponsor_no_dead_tribute():
    ed = _shop_ed()
    engine.apply_purchase(ed, "50", "1", "weapon", 100000, "d1", 0)
    assert "already has" in engine.purchase_error(ed, "51", "1", "weapon", "d1")
    assert "yourself" in engine.purchase_error(ed, "2", "2", "water", "d1")
    ed["roster"]["3"]["alive"] = False
    assert "no longer" in engine.purchase_error(ed, "50", "3", "water", "d1")


def test_prices_follow_the_day():
    tue = datetime(2026, 12, 1, 13, 0, tzinfo=HOME_TZ).timestamp()
    sat = datetime(2026, 12, 5, 13, 0, tzinfo=HOME_TZ).timestamp()
    assert engine.unit_price(1500, tue) == 1500
    assert engine.unit_price(1500, sat) == 2300   # 2250 rounds to the nearest 100
    assert engine.unit_price(100000, sat, test=True) == 100000


def test_shop_only_between_scores_and_finale_and_not_during_a_broadcast():
    ed = _shop_ed()
    names = [s for s, _ in ed["schedule"]]
    ed["step"] = names.index("scores")
    assert not engine.shop_open(ed)
    ed["step"] = names.index("scores") + 1
    assert engine.shop_open(ed)
    ed["broadcast"] = {"queue": [{"t": "line", "text": "x"}], "pos": 0}
    assert not engine.shop_open(ed)
    ed["broadcast"] = None
    ed["step"] = len(names)
    assert not engine.shop_open(ed)


def test_prize_floor_and_mint_cap():
    assert engine.prize(450000, 0.15, 10000) == (67500, 0)
    assert engine.prize(20000, 0.15, 10000) == (10000, 7000)
    assert engine.prize(0, 0.15, 10000) == (10000, 10000)


# ---------------------------------------------------------------- training


def test_training_score_scale():
    assert engine.training_score(0) == 1
    assert engine.training_score(400) == 12
    assert engine.training_score(200) == 6
    assert engine.training_score(-50) == 1


def test_scoring_speed_is_relative_and_missing_scores_zero():
    roster = {
        "a": engine.new_tribute("A", "volunteer"),
        "b": engine.new_tribute("B", "volunteer"),
        "c": engine.new_tribute("C", "volunteer"),
    }
    roster["a"]["challenges"]["agility"] = {"acc": 70, "time": 10, "finished": True}
    roster["b"]["challenges"]["agility"] = {"acc": 70, "time": 20, "finished": True}
    roster["c"]["challenges"]["agility"] = {"acc": 40, "time": 45, "finished": False}
    engine.score_challenges(roster)
    assert roster["a"]["stats"]["agility"] == 100
    assert roster["b"]["stats"]["agility"] == 85
    assert roster["c"]["stats"]["agility"] == 40
    assert roster["a"]["stats"]["strength"] == 0
    assert roster["a"]["score"] == engine.training_score(100)


def test_accuracy_formulas():
    assert engine.agility_accuracy(0) == 70 and engine.agility_accuracy(7) == 0
    assert engine.strength_accuracy(8) == 70 and engine.strength_accuracy(4) == 35
    assert engine.endurance_accuracy(12) == 70 and engine.endurance_accuracy(2) == 0
    assert engine.intellect_accuracy(3, 0) == 70 and engine.intellect_accuracy(0, 2) == pytest.approx(70 / 3)


def test_charisma_relative_to_most_cheered():
    assert engine.charisma_scores({"a": [1, 2, 3, 4], "b": [1, 2]}, ["a", "b", "c"]) == {"a": 100, "b": 50, "c": 0}
    assert engine.charisma_scores({}, ["a", "b"]) == {"a": 50, "b": 50}


def test_cipher_answers_normalize():
    p = {"answers": ["map"]}
    assert engine.check_answer(p, "A MAP") and engine.check_answer(p, "map.") and engine.check_answer(p, "the map")
    assert engine.check_answer({"answers": ["TRACKER JACKER"]}, "trackerjacker")
    assert not engine.check_answer(p, "") and not engine.check_answer(p, "mop")


def test_puzzles_are_distinct_within_an_edition():
    rng = random.Random(5)
    used = []
    for _ in range(12):
        ps = engine.pick_puzzles(CIPHERS, rng, used)
        assert len({p["id"] for p in ps}) == 3
        used += [p["id"] for p in ps]
    assert len(used) == len(set(used))


def test_scrambles_and_codes_decode_to_the_answer():
    rng = random.Random(1)
    for word in CIPHERS["words"]:
        s = engine.make_puzzle("scramble", word, rng)
        assert sorted(s["prompt"].replace(" ", "").replace("/", "")) == sorted(word.replace(" ", ""))
        c = engine.make_puzzle("caesar", word, rng)
        shift = int(c["kind"].split("back by ")[1])
        assert engine.caesar(c["prompt"], -shift) == word


# ---------------------------------------------------------------- schedule and draft


def test_schedule_is_dst_safe():
    for sunday in (date(2026, 3, 8), date(2026, 11, 1), date(2026, 11, 22)):   # both DST switch days and a normal week
        sched = engine.week_schedule(sunday)
        for step, ts in sched:
            local = datetime.fromtimestamp(ts, HOME_TZ)
            assert (local.hour, local.minute) in ((10, 0), (21, 0), (23, 59), (8, 0), (12, 0), (19, 0)), (step, local)
        assert datetime.fromtimestamp(sched[0][1], HOME_TZ).date() == sunday


def test_next_reaping_skips_a_week_after_the_second_reaping():
    sun = date(2026, 11, 22)
    sched = engine.week_schedule(sun)
    before = engine.step_ts(sched, "second_reaping") - 60
    after = engine.step_ts(sched, "second_reaping") + 60
    assert engine.next_reaping(before)[0] == sun
    assert engine.next_reaping(after)[0] == date(2026, 11, 29)
    saturday = datetime(2026, 11, 21, 12, 0, tzinfo=HOME_TZ).timestamp()
    assert engine.next_reaping(saturday)[0] == sun


def test_draft_pool_rules():
    people = [
        {"uid": 1, "messages": 50}, {"uid": 2, "messages": 5}, {"uid": 3, "messages": 50, "lurker": True},
        {"uid": 4, "messages": 50, "opt_out": True}, {"uid": 5, "messages": 50, "bot": True},
        {"uid": 6, "messages": 80}, {"uid": 7, "messages": 90},
    ]
    pool = engine.draft_pool(people, {"7": {}}, min_messages=20, last_tributes=[6], withdrawn=[])
    assert pool == {"1": 50}


def test_draw_weighting_has_diminishing_returns():
    rng = random.Random(3)
    wins = {"big": 0, "small": 0}
    for _ in range(4000):
        wins[engine.weighted_draw({"big": 10000, "small": 25}, 1, rng)[0]] += 1
    # sqrt(200)=14.1 vs sqrt(25)=5: about 74% / 26%, nowhere near 400:1
    assert 0.68 < wins["big"] / 4000 < 0.80


def test_draw_never_repeats():
    rng = random.Random(4)
    out = engine.weighted_draw({str(i): 30 + i for i in range(10)}, 4, rng)
    assert len(out) == len(set(out)) == 4


def test_withdrawal_seat_goes_to_first_pending_requester():
    ed, _ = make_edition(4, 1, items=False)
    ed["swap_requests"]["0"] = {"volunteers": ["50", "51"], "message_id": 1}
    assert engine.seat_for_withdrawal(ed, "0") == "50"
    assert engine.seat_for_withdrawal(ed, "1") is None


def test_ordinals():
    assert [engine.ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"]


def test_event_pool_shape():
    for t in POOL["templates"]:
        for key in ("id", "text", "phases", "size", "killers", "victims", "type", "tags"):
            assert key in t, t
        assert "(Elimination" not in t["text"]
        if t["type"] == "combat":
            assert t["killers"] and t["victims"]
        if t["type"] == "hazard" and t["victims"]:
            assert not t["killers"] and t.get("hazard")
    assert len(POOL["templates"]) >= 300
    assert len(CIPHERS["words"]) * 2 + len(CIPHERS["riddles"]) >= 60
    assert all(k in ITEMS for k in ("water", "med_kit", "weapon"))
