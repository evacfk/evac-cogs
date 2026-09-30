import random
from datetime import datetime

from . import engine


def test_success_text():
    assert engine.is_success_text("Bump done! :thumbsup:\nCheck it out on DISBOARD.")
    assert engine.is_success_text("BUMP  DONE!")
    assert not engine.is_success_text("Please wait another 37 minutes until the server can be bumped")
    assert not engine.is_success_text("")
    assert not engine.is_success_text(None)


def test_cooldown_parse():
    assert engine.parse_cooldown_seconds("Please wait another 37 minutes until the server can be bumped") == 37 * 60
    assert engine.parse_cooldown_seconds("wait another 1 minute until") == 60
    assert engine.parse_cooldown_seconds("wait another 2 hours") == 7200
    assert engine.parse_cooldown_seconds("Bump done!") is None
    assert engine.parse_cooldown_seconds("") is None


def test_day_key_uses_la_time():
    # 2026-10-01 05:00 UTC is still 2026-09-30 22:00 in LA (PDT)
    utc = datetime.fromisoformat("2026-10-01T05:00:00+00:00")
    assert engine.day_key(utc) == "2026-09-30"
    assert engine.day_key(utc.timestamp()) == "2026-09-30"
    # 08:00 UTC = 01:00 LA the next day
    assert engine.day_key(datetime.fromisoformat("2026-10-01T08:00:00+00:00")) == "2026-10-01"


def test_week_key_is_iso_week_in_la_time():
    mon = datetime.fromisoformat("2026-09-28T12:00:00-07:00")   # Monday
    sun = datetime.fromisoformat("2026-09-27T12:00:00-07:00")   # previous Sunday
    assert engine.week_key(mon) == "2026-W40"
    assert engine.week_key(sun) == "2026-W39"
    # Sunday 23:30 LA is already Monday in UTC, but the LA week hasn't rolled yet
    late_sun = datetime.fromisoformat("2026-09-28T06:30:00+00:00")
    assert engine.week_key(late_sun) == "2026-W39"
    # ISO year boundary: 2027-01-01 (Fri) belongs to 2026-W53
    assert engine.week_key(datetime.fromisoformat("2027-01-01T12:00:00-08:00")) == "2026-W53"


def test_month_key_uses_la_time():
    utc = datetime.fromisoformat("2026-10-01T05:00:00+00:00")    # still Sep 30 in LA
    assert engine.month_key(utc) == "2026-09"
    assert engine.month_key(datetime.fromisoformat("2026-10-01T08:00:00+00:00")) == "2026-10"


def test_update_run_extends_only_for_same_bumper():
    assert engine.update_run(0, 0, 42) == 1        # nobody before
    assert engine.update_run(42, 1, 42) == 2       # same person again
    assert engine.update_run(42, 4, 42) == 5
    assert engine.update_run(42, 4, 43) == 1       # someone else interrupts
    assert engine.update_run(42, 0, 42) == 2       # corrupt/zero run treated as 1 before extending


def test_streak_bonus():
    assert engine.streak_bonus_pct(1, 10, 5) == 0
    assert engine.streak_bonus_pct(2, 10, 5) == 10
    assert engine.streak_bonus_pct(6, 10, 5) == 50
    assert engine.streak_bonus_pct(40, 10, 5) == 50   # capped
    assert engine.streak_bonus_pct(0, 10, 5) == 0
    assert engine.streak_bonus_pct(3, 10, 0) == 0


def test_period_buckets():
    assert engine.bump_period(None, "2026-W40") == {}
    assert engine.bump_period({"key": "2026-W39", "counts": {"1": 5}}, "2026-W40") == {}   # stale
    assert engine.bump_period({"key": "2026-W40", "counts": {"1": 5}}, "2026-W40") == {"1": 5}
    b = engine.add_bump(None, "2026-W40", 7)
    assert b == {"key": "2026-W40", "counts": {"7": 1}}
    b = engine.add_bump(b, "2026-W40", 7)
    b = engine.add_bump(b, "2026-W40", 8)
    assert b["counts"] == {"7": 2, "8": 1}
    # new period discards the old counts
    assert engine.add_bump(b, "2026-W41", 8) == {"key": "2026-W41", "counts": {"8": 1}}
    # input bucket is not mutated
    assert b["counts"] == {"7": 2, "8": 1}


def test_reward():
    rng = random.Random(1)
    vals = {engine.roll_reward(100, 250, rng) for _ in range(500)}
    assert min(vals) >= 100 and max(vals) <= 250
    assert engine.roll_reward(250, 100, random.Random(2)) in range(100, 251)  # swapped bounds tolerated
    assert engine.roll_reward(5, 5) == 5
    assert engine.final_reward(200, 0) == 200
    assert engine.final_reward(200, 50) == 300
    assert engine.final_reward(101, 10) == 111


def test_rank_counts():
    ranked = engine.rank_counts({"2": 3, "1": 3, "3": 5, "4": 1}, 3)
    assert ranked == [("3", 5), ("1", 3), ("2", 3)]


def test_format_duration():
    assert engine.format_duration(45) == "45s"
    assert engine.format_duration(125) == "2m 5s"
    assert engine.format_duration(7260) == "2h 1m"
    assert engine.format_duration(1800) == "30m"
    assert engine.format_duration(7200) == "2h"
    assert engine.format_duration(5400) == "1h 30m"


def test_cooldown_seconds_free_is_always_two_hours():
    assert engine.cooldown_seconds("free", 0) == 7200
    assert engine.cooldown_seconds("free", 1) == 7200
    assert engine.cooldown_seconds("free", 50) == 7200


def test_cooldown_seconds_pro_is_30_min_under_12_bumps():
    assert engine.cooldown_seconds("pro", 1) == 1800
    assert engine.cooldown_seconds("pro", 11) == 1800      # fewer than 12 -> fast
    assert engine.cooldown_seconds("pro", 12) == 7200      # the 12th bump uses up the allowance
    assert engine.cooldown_seconds("pro", 30) == 7200


def test_prune_recent_keeps_only_last_24h():
    now = 1_000_000.0
    bumps = [now - 86400 - 1, now - 86400 + 1, now - 100, now + 5000]   # too old, just inside, recent, future (ignored)
    assert engine.prune_recent(bumps, now) == [now - 86400 + 1, now - 100]
    assert engine.prune_recent(None, now) == []


def test_bump_latency_clamped():
    assert engine.bump_latency(100.0, None) == 0.0
    assert engine.bump_latency(100.0, 98.0) == 2.0
    assert engine.bump_latency(100.0, 101.0) == 0.0      # clock skew: never negative
    assert engine.bump_latency(100.0, 0.0) == engine.MAX_LATENCY_COMP_SECONDS
