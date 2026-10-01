from datetime import date

import pytest

from serverpulse import models, storage
from serverpulse._testdata import local_ts
from serverpulse.tracker import GuildTracker

T0 = local_ts(2026, 9, 29, 20, 0, 0)


def test_missing_day_is_empty_doc(tmp_path):
    assert storage.load_day(tmp_path, 1, "2026-09-29") == models.new_day_doc()


def test_roundtrip_and_listing(tmp_path):
    doc = models.new_day_doc()
    doc["joins"] = 3
    storage.save_day(tmp_path, 1, "2026-09-29", doc)
    storage.save_day(tmp_path, 1, "2026-09-30", doc)
    assert storage.load_day(tmp_path, 1, "2026-09-29")["joins"] == 3
    assert storage.list_dates(tmp_path, 1) == ["2026-09-29", "2026-09-30"]
    assert not list((tmp_path / "days" / "1").glob("*.tmp"))  # atomic write leaves no temp file


def test_load_range_skips_missing_days(tmp_path):
    storage.save_day(tmp_path, 1, "2026-09-29", models.new_day_doc())
    got = storage.load_range(tmp_path, 1, date(2026, 9, 28), date(2026, 9, 30))
    assert list(got) == ["2026-09-29"]


def test_bad_date_key_rejected(tmp_path):
    with pytest.raises(ValueError):
        storage.day_path(tmp_path, 1, "../escape")


def test_apply_snapshot_replaces_hours_but_adds_counters(tmp_path):
    tr = GuildTracker(floor_ts=T0)
    tr.record(T0 + 5, 11, "111")
    tr.record(T0 + 9, 12, "111")
    tr.record_join(T0 + 9)
    snap = tr.snapshot(T0 + 100)
    storage.apply_snapshot(tmp_path, 1, snap)
    tr.commit(snap)

    tr.record(T0 + 200, 11, "111")
    snap2 = tr.snapshot(T0 + 300)
    storage.apply_snapshot(tmp_path, 1, snap2)
    tr.commit(snap2)

    doc = storage.load_day(tmp_path, 1, "2026-09-29")
    assert doc["h"]["20"]["m"] == 3  # absolute record, not 2 + 3
    assert doc["u"] == {"11": 2, "12": 1}  # deltas summed
    assert doc["joins"] == 1


def test_purge_user_counts_keeps_hourly_totals(tmp_path):
    doc = models.new_day_doc()
    doc["h"]["20"] = {"m": 5}
    doc["u"] = {"1": 5}
    doc["ub"] = {"2": 3}
    storage.save_day(tmp_path, 1, "2026-01-01", doc)
    storage.save_day(tmp_path, 1, "2026-09-29", doc)
    assert storage.purge_user_counts(tmp_path, 1, date(2026, 6, 1)) == 1
    old = storage.load_day(tmp_path, 1, "2026-01-01")
    assert old["u"] == {} and old["ub"] == {} and old["h"]["20"]["m"] == 5
    assert storage.load_day(tmp_path, 1, "2026-09-29")["u"] == {"1": 5}


def test_delete_user_removes_only_that_user(tmp_path):
    doc = models.new_day_doc()
    doc["u"] = {"1": 5, "2": 6}
    doc["ub"] = {"1": 1}
    storage.save_day(tmp_path, 1, "2026-09-29", doc)
    assert storage.delete_user(tmp_path, 1, 1) == 1
    got = storage.load_day(tmp_path, 1, "2026-09-29")
    assert got["u"] == {"2": 6} and got["ub"] == {}


def test_open_state_roundtrip(tmp_path):
    assert storage.load_open_state(tmp_path, 1) == {}
    storage.save_open_state(tmp_path, 1, {"slots": [{"date": "2026-09-29", "key": "20", "users": [1]}]})
    assert storage.load_open_state(tmp_path, 1)["slots"][0]["users"] == [1]


def test_corrupt_open_state_is_ignored(tmp_path):
    p = storage.open_state_path(tmp_path, 1)
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert storage.load_open_state(tmp_path, 1) == {}
