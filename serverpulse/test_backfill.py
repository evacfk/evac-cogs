from datetime import date

from serverpulse import backfill as bf
from serverpulse import models, storage
from serverpulse._testdata import hour_rec, local_ts
from serverpulse.tracker import GuildTracker

START = int(local_ts(2026, 9, 27))
END = int(local_ts(2026, 9, 30, 12))  # "live_since"


def rows():
    return [
        (local_ts(2026, 9, 28, 20, 10), 1, "111"),
        (local_ts(2026, 9, 28, 20, 20), 2, "111"),
        (local_ts(2026, 9, 28, 20, 25), 1, "222"),
        (local_ts(2026, 9, 29, 21, 0, 30), 3, "111"),
        (local_ts(2026, 9, 30, 11, 59, 59), 4, "222"),
    ]


class TestStage:
    def test_roundtrip_sorted_and_merged_across_channels(self, tmp_path):
        stage = bf.Stage(tmp_path, 1)
        stage.start({"days": 3})
        stage.write_channel("111", [(30.0, 1), (10.0, 2)])
        stage.write_channel("222", [(20.0, 3), (40.0, 4)])
        assert stage.done_channels() == {"111", "222"}
        assert [(r[0], r[1], r[2]) for r in stage.merged_rows()] == [(10.0, 2, "111"), (20.0, 3, "222"), (30.0, 1, "111"), (40.0, 4, "222")]

    def test_unfinished_channel_leaves_no_done_marker(self, tmp_path):
        stage = bf.Stage(tmp_path, 1)
        stage.start({"days": 3})
        (stage.dir / "333.tmp").write_bytes(b"partial")
        assert stage.done_channels() == set()

    def test_start_wipes_previous_run(self, tmp_path):
        stage = bf.Stage(tmp_path, 1)
        stage.start({"days": 3})
        stage.write_channel("111", [(1.0, 1)])
        stage.start({"days": 7})
        assert stage.done_channels() == set() and stage.meta() == {"days": 7}

    def test_meta_missing_is_none(self, tmp_path):
        assert bf.Stage(tmp_path, 9).meta() is None

    def test_empty_channel_file_is_fine(self, tmp_path):
        stage = bf.Stage(tmp_path, 1)
        stage.start({})
        stage.write_channel("111", [])
        assert list(stage.merged_rows()) == []


class TestBuildDays:
    def test_matches_live_tracking_exactly(self):
        """Backfilled and live numbers must mean the same thing."""
        live = GuildTracker(floor_ts=START)
        for ts, uid, ck in rows():
            live.record(ts, uid, ck)
        live_snap = live.snapshot(END)
        docs = bf.build_days(rows(), START, END)
        for dk, key, rec, _v, _c in live_snap.hours:
            assert docs[dk]["h"][key] == rec

    def test_user_counts_go_to_ub_not_u(self):
        docs = bf.build_days(rows(), START, END)
        assert docs["2026-09-28"]["ub"] == {"1": 2, "2": 1} and docs["2026-09-28"]["u"] == {}

    def test_rows_outside_the_window_are_ignored(self):
        extra = rows() + [(START - 100.0, 9, "111"), (END + 100.0, 9, "111")]
        docs = bf.build_days(extra, START, END)
        assert all("9" not in d["ub"] for d in docs.values())

    def test_no_rows_no_days(self):
        assert bf.build_days([], START, END) == {}


class TestApply:
    def test_replaces_window_hours_and_leaves_live_hours_alone(self, tmp_path):
        stale = models.new_day_doc()
        stale["h"]["20"] = hour_rec(999)  # old backfill (or garbage) inside the window
        stale["h"]["13"] = hour_rec(7)  # 1pm on live_since's day: live territory (>= END)
        stale["u"] = {"5": 7}  # live user counts must survive
        stale["ub"] = {"8": 99}
        storage.save_day(tmp_path, 1, "2026-09-30", stale)
        storage.save_day(tmp_path, 1, "2026-09-28", {**models.new_day_doc(), "h": {"20": hour_rec(999), "03": hour_rec(5)}})

        docs = bf.build_days(rows(), START, END)
        bf.apply_backfill(tmp_path, 1, docs, START, END)

        sep28 = storage.load_day(tmp_path, 1, "2026-09-28")
        assert sep28["h"]["20"]["m"] == 3  # replaced, not 999 and not added
        assert "03" not in sep28["h"]  # stale hour inside the window with no fresh data is cleared
        sep30 = storage.load_day(tmp_path, 1, "2026-09-30")
        assert sep30["h"]["13"]["m"] == 7  # at/after live_since: untouched
        assert sep30["u"] == {"5": 7}
        assert sep30["h"]["11"]["m"] == 1  # 11:59:59 is before live_since: backfilled
        assert sep30["ub"] == {"4": 1}  # ub replaced, stale {"8": 99} gone

    def test_rerun_is_idempotent(self, tmp_path):
        docs = bf.build_days(rows(), START, END)
        bf.apply_backfill(tmp_path, 1, docs, START, END)
        first = {d: storage.load_day(tmp_path, 1, d) for d in storage.list_dates(tmp_path, 1)}
        bf.apply_backfill(tmp_path, 1, docs, START, END)
        again = {d: storage.load_day(tmp_path, 1, d) for d in storage.list_dates(tmp_path, 1)}
        assert first == again

    def test_empty_days_in_window_are_not_written(self, tmp_path):
        bf.apply_backfill(tmp_path, 1, {}, START, END)
        assert storage.list_dates(tmp_path, 1) == []
