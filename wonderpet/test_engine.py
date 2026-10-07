"""Pure-logic tests for wonderpet's engine (no discord, no redbot)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from wonderpet import engine
from wonderpet.constants import ADULT_RETIRE_DAYS, GONE_HOURS, MAX_CATCHUP_HOURS

LA = ZoneInfo("America/Los_Angeles")
H = 3600


def pet(stage="baby", now=0.0):
    p = engine.new_pet(now, 1, "Tester")
    p["stage"] = stage
    return p


def care(p, n, uid_base=100):
    for i in range(n):
        engine.give_care(p, "feed", uid_base + i)


def test_egg_does_not_decay_and_never_gets_sick():
    p = engine.new_pet(0, 1, "T")
    assert engine.advance(p, 5 * H) == []
    assert (p["hunger"], p["happy"], p["clean"]) == (80, 80, 80)
    assert p["neglect_hours"] == 0 and engine.level_for(p) == 0


def test_egg_hatches_only_with_both_time_and_care():
    p = engine.new_pet(0, 1, "T")
    care(p, 6)
    assert engine.advance(p, 11 * H) == [] and p["stage"] == "egg"        # enough care, too early
    q = engine.new_pet(0, 2, "T")
    care(q, 5)
    assert engine.advance(q, 13 * H) == [] and q["stage"] == "egg"        # enough time, too little care
    care(q, 1, 900)
    assert engine.advance(q, 13 * H + 60) == ["hatched"] and q["stage"] == "baby"
    assert q["stage_care"] == 0


def test_decay_rates_and_floor():
    p = pet()
    engine.advance(p, 2 * H)
    assert (p["hunger"], p["happy"], p["clean"]) == (75, 76, 77)
    engine.advance(p, 2 * H + 5 * H)
    p["last_tick_ts"] = 0
    engine.advance(p, 6 * H)
    assert min(p["hunger"], p["happy"], p["clean"]) >= 0


def test_long_gap_only_ages_a_few_hours():
    p = pet()
    engine.advance(p, 100 * H)  # a restart / outage: the pet is not punished for it
    assert p["hunger"] == 80 - 2.5 * MAX_CATCHUP_HOURS
    assert p["neglect_hours"] == 0 and p["alive"]


def run_hours(p, hours, start=0, step=1):
    """Advance hour by hour with no care; return [(hour, event)]."""
    log = []
    for h in range(start + step, start + hours + 1, step):
        for ev in engine.advance(p, h * H):
            log.append((h, ev))
        if not p["alive"]:
            break
    return log


def test_neglect_escalates_with_days_of_warning_before_loss():
    p = pet()
    for m in ("hunger", "happy", "clean"):
        p[m] = 100.0
    log = run_hours(p, 200)
    names = [e for _, e in log]
    assert names[:5] == ["warn:1", "warn:2", "warn:3", "warn:4", "lost:died"]
    first = dict((e, h) for h, e in log)
    assert first["warn:1"] < first["warn:2"] < first["warn:3"] < first["warn:4"] < first["lost:died"]
    assert first["lost:died"] - first["warn:2"] >= 48           # sick -> gone gives at least two full days
    assert first["lost:died"] - first["warn:4"] >= 10           # final warning has half a day on the clock
    assert first["lost:died"] >= 100                            # from a healthy pet: four+ days of silence
    assert p["ended"]["kind"] == "died" and not p["alive"]


def test_runs_away_when_it_was_unhappiness_not_hunger():
    p = pet()
    p.update(hunger=100.0, happy=0.0, clean=0.0)
    p["neglect_hours"] = GONE_HOURS - 0.5
    p["weak"] = "happy"
    p["happy"], p["clean"], p["hunger"] = 0.0, 0.0, 0.0
    p["hunger"] = 40.0   # hunger is not the lowest meter
    events = engine.advance(p, 2 * H)
    assert "lost:ran_away" in events
    assert p["ended"]["kind"] == "ran_away"


def test_care_rescues_a_failing_pet():
    p = pet()
    p.update(hunger=5.0, happy=5.0, clean=5.0, neglect_hours=40.0, warn_level=3)
    for kind in ("feed", "play", "clean"):
        for i in range(4):
            engine.give_care(p, kind, i)   # 4 x +20 on each meter
    ev = engine.advance(p, 1 * H)
    assert "recovered" in ev and p["alive"]
    assert engine.level_for(p) == 0


def test_recovery_burns_down_the_clock_twice_as_fast():
    p = pet()
    p["neglect_hours"] = 10.0
    p.update(hunger=100.0, happy=100.0, clean=100.0)
    engine.advance(p, 1 * H)
    assert p["neglect_hours"] == 8.0


def test_growth_is_paused_while_sick():
    p = pet("baby")
    p["warn_level"] = 2
    p["neglect_hours"] = 20.0
    p.update(hunger=0.0, happy=0.0, clean=0.0)
    p["stage_care"] = 999
    p["last_tick_ts"] = 8 * 24 * H
    assert engine.advance(p, 8 * 24 * H + 60) == [] or p["stage"] == "baby"
    assert p["stage"] == "baby"


def test_baby_and_teen_stages_need_time_and_care():
    p = pet("baby")
    p["stage_care"] = 80
    p.update(hunger=100.0, happy=100.0, clean=100.0)
    p["last_tick_ts"] = 0
    assert engine.advance(p, 6 * 24 * H) == [] and p["stage"] == "baby"   # care yes, time no
    p.update(hunger=100.0, happy=100.0, clean=100.0)
    assert engine.advance(p, 7 * 24 * H + 60) == ["grew:teen"] and p["stage"] == "teen"


def test_adult_form_follows_how_well_it_was_raised():
    for quality, form in ((90, "radiant"), (70, "radiant"), (69.9, "happy"), (45, "happy"), (44.9, "scruffy"), (10, "scruffy")):
        p = pet("teen")
        p["quality_sum"], p["quality_hours"] = quality * 100.0, 100.0
        assert engine.adult_form(p) == form


def test_teen_becomes_adult_with_a_form():
    p = pet("teen")
    p["stage_care"] = 200
    p["quality_sum"], p["quality_hours"] = 80.0 * 50, 50.0
    p.update(hunger=100.0, happy=100.0, clean=100.0)
    p["last_tick_ts"] = 10 * 24 * H - 60
    assert engine.advance(p, 10 * 24 * H) == ["grew:adult"]
    assert p["stage"] == "adult" and p["form"] == "radiant"


def test_adult_retires_after_three_weeks():
    p = pet("adult")
    p["form"] = "happy"
    p.update(hunger=100.0, happy=100.0, clean=100.0)
    p["last_tick_ts"] = ADULT_RETIRE_DAYS * 86400 - 60
    ev = engine.advance(p, ADULT_RETIRE_DAYS * 86400)
    assert ev == ["retired"] and p["ended"]["kind"] == "retired"


def test_care_is_capped_at_100_and_credited_to_the_carer():
    p = pet()
    p["hunger"] = 95.0
    engine.give_care(p, "feed", 7)
    assert p["hunger"] == 100.0
    engine.give_treat(p, "feast", 7)
    assert (p["happy"], p["clean"]) == (100.0, 100.0)
    assert p["carers"] == {"7": 2} and p["care_points"] == 3 and p["stage_care"] == 3


def test_treat_prices_scale():
    assert engine.treat_price("snack", 2000) == 2000
    assert engine.treat_price("feast", 2000) == 8000
    assert engine.treat_price("snack", 10000) == 10000 and engine.treat_price("feast", 10000) == 40000


def test_daily_limits_reset_at_pacific_midnight():
    t1 = datetime(2026, 10, 10, 23, 59, tzinfo=LA).timestamp()
    t2 = datetime(2026, 10, 11, 0, 1, tzinfo=LA).timestamp()
    d = engine.daily_for(None, engine.local_date(t1).isoformat())
    d["used"] = "feed"
    assert engine.daily_for(d, engine.local_date(t1).isoformat())["used"] == "feed"
    assert engine.daily_for(d, engine.local_date(t2).isoformat())["used"] is None


def test_daily_reads_the_old_per_action_flags_so_nobody_gets_a_second_free_care_on_deploy_day():
    today = "2026-10-07"
    old = {"date": today, "feed": False, "play": True, "clean": False, "treats": 2}
    d = engine.daily_for(old, today)
    assert d["used"] == "play" and d["treats"] == 2


def test_weeks_roll_over_and_old_weeks_are_dropped():
    wc: dict = {}
    base = datetime(2026, 10, 5, 12, 0, tzinfo=LA).timestamp()          # a Monday
    engine.record_week(wc, base, 1)
    engine.record_week(wc, base + 6 * 86400, 1)                         # the Sunday: same week
    assert list(wc.values()) == [{"1": 2}]
    engine.record_week(wc, base + 7 * 86400, 2)                         # next Monday: new week
    assert len(wc) == 2
    for w in range(2, 8):
        engine.record_week(wc, base + w * 7 * 86400, 3)
    assert len(wc) == 4


def test_top_carers_ranks_and_breaks_ties_by_id():
    assert engine.top_carers({"5": 3, "2": 3, "9": 10, "4": 1}, 3) == [(9, 10), (2, 3), (5, 3)]
    assert engine.top_carers(None) == []


def test_forget_user_scrubs_everything():
    p = pet()
    engine.give_care(p, "feed", 5)
    wc = {"2026-W41": {"5": 3, "6": 1}}
    hist = [{"carers": {"5": 2}}]
    engine.forget_user(p, wc, hist, 5)
    assert p["carers"] == {} and wc == {"2026-W41": {"6": 1}} and hist == [{"carers": {}}]


def test_history_entry_captures_top_carers():
    p = pet("adult")
    p["form"] = "happy"
    for uid, n in ((1, 3), (2, 5), (3, 1), (4, 4)):
        for _ in range(n):
            engine.give_care(p, "feed", uid)
    p["ended"] = {"kind": "retired", "ts": 99.0}
    e = engine.history_entry(p, 99.0)
    assert [t["user"] for t in e["top"]] == [2, 4, 1] and e["end"] == "retired" and e["end_ts"] == 99.0


def test_relapse_resumes_the_clock_instead_of_resetting_it():
    p = pet()
    p.update(hunger=5.0, happy=5.0, clean=5.0, neglect_hours=40.0, warn_level=3)
    for kind in ("feed", "play", "clean"):
        for i in range(4):
            engine.give_care(p, kind, i)
    engine.advance(p, 1 * H)
    assert engine.level_for(p) == 0 and p["neglect_hours"] == 38.0
    p.update(hunger=1.0, happy=1.0, clean=1.0)      # everyone wanders off again
    ev = engine.advance(p, 2 * H)
    assert "warn:3" in ev   # straight back to critical, not "worried" again
