import json
from datetime import date, timedelta

from serverpulse import export
from serverpulse._testdata import traffic


def build(**kw):
    docs, cov, end = traffic(date(2026, 9, 1), 28, seed=3)
    args = dict(
        guild_id=1, guild_name="Wonderland", now_ts=end, coverage_start_ts=cov, days=docs,
        start=date(2026, 9, 1), end=date(2026, 9, 28), raw_start=date(2026, 9, 22),
        channel_names={"111": "general", "222": "memes"}, settings={"ignored_channels": [9]},
    )
    args.update(kw)
    return build_export_(**args)


build_export_ = export.build_export


def test_schema_and_top_level_keys_are_stable():
    out = build()
    assert out["schema"] == "serverpulse.export.v1"
    for key in ("generated_at", "guild", "range", "coverage", "definitions", "hour_of_day", "weekday_hour_matrix",
                "best_windows_2h", "quietest_windows_2h", "longest_dead_stretch", "anomalies_last_30_days",
                "daily", "weekly", "monthly", "channels", "hourly", "settings"):
        assert key in out, key
    assert out["guild"] == {"id": "1", "name": "Wonderland"}


def test_is_json_serialisable():
    json.dumps(build())


def test_hour_of_day_has_24_rows_and_matrix_is_7x24():
    out = build()
    assert [r["hour"] for r in out["hour_of_day"]] == list(range(24))
    m = out["weekday_hour_matrix"]
    assert len(m["avg_messages"]) == 7 and all(len(r) == 24 for r in m["avg_messages"])
    assert m["weekdays"][0] == "Monday"


def test_daily_weekly_monthly_rollups_agree():
    out = build()
    total = sum(d["messages"] for d in out["daily"])
    assert sum(w["messages"] for w in out["weekly"]) == total
    assert sum(m["messages"] for m in out["monthly"]) == total
    assert out["monthly"][0]["period"] == "2026-09"


def test_hourly_detail_respects_raw_start_and_names_channels():
    out = build()
    assert out["hourly"] and min(h["date"] for h in out["hourly"]) >= "2026-09-22"
    assert out["channels"][0]["name"] in ("general", "memes", None)
    assert set(out["hourly"][0]["channels"]) <= {"111", "222", "333"}


def test_users_are_opt_in():
    assert "top_chatters" not in build()
    out = build(include_users=True, user_names={1: "Ann"})
    assert len(out["top_chatters"]) <= 25 and out["top_chatters"][0]["user_id"].isdigit()


def test_empty_server_exports_cleanly():
    out = export.build_export(guild_id=1, guild_name="x", now_ts=1.0e9, coverage_start_ts=None, days={},
                              start=date(2026, 9, 1), end=date(2026, 9, 2), raw_start=date(2026, 9, 1))
    json.dumps(out)
    assert out["daily"] == [] and out["longest_dead_stretch"] is None
