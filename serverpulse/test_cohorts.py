"""Pure retention logic: records, invite diffing, log parsing, the funnel numbers."""
from datetime import date

from serverpulse import cohorts
from serverpulse._testdata import local_ts
from serverpulse.cohort_store import CohortStore

UID = 1100000000000000000  # a 2022-ish snowflake


def test_snowflake_timestamp():
    assert abs(cohorts.snowflake_ts(175928847299117063) - 1462015105.796) < 1  # discord.py docs example


def test_record_message_tracks_days_first_message_and_window():
    j = local_ts(2026, 9, 1, 23, 50)
    rec = cohorts.new_record(UID, j)
    assert not cohorts.record_message(rec, j - 5)  # before joining
    assert cohorts.record_message(rec, j + 60)
    assert cohorts.record_message(rec, local_ts(2026, 9, 2, 0, 10))  # next local day, 20 minutes later
    assert cohorts.record_message(rec, local_ts(2026, 9, 9, 12))
    assert not cohorts.record_message(rec, local_ts(2026, 10, 20, 12))  # past the 45-day window
    assert rec["d"] == [0, 1, 8] and rec["n"] == 3 and rec["fm"] == j + 60


def test_record_message_ignores_messages_after_leaving():
    j = local_ts(2026, 9, 1, 12)
    rec = cohorts.new_record(UID, j)
    cohorts.record_leave(rec, j + 300, onboarding=False, roles=0)
    assert not cohorts.record_message(rec, j + 400)


def test_invite_diff_and_labels():
    before = {"dis": 10, "me": 4, "evac": 2}
    assert cohorts.invite_diff(before, {"dis": 11, "me": 4, "evac": 2}) == ["dis"]
    assert cohorts.invite_diff(before, {"dis": 10, "me": 4, "evac": 2, "new": 1}) == ["new"]
    assert cohorts.invite_diff(before, {"dis": 11, "me": 5, "evac": 2}) == ["dis", "me"]  # ambiguous
    labels = {"dis": "Disboard"}
    assert cohorts.source_label("dis", labels) == "Disboard"
    assert cohorts.source_label("evac", labels, "evac") == "evac's invite"
    assert cohorts.source_label(None, labels) == "Unknown"
    assert cohorts.source_label("vanity", labels) == "Vanity URL"


def test_log_text_parsing():
    assert cohorts.ids_in_text(["Welcome <@!123456789012345678> to Wonderland!"]) == [123456789012345678]
    assert cohorts.ids_in_text(["someone left", "ID: 123456789012345678"]) == [123456789012345678]
    assert cohorts.ids_in_text(["role <@&123456789012345678> channel <#123456789012345678>"]) == []
    assert cohorts.classify_text("evac joined the server") == "join"
    assert cohorts.classify_text("Welcome to Wonderland!") == "join"
    assert cohorts.classify_text("evac left the server") == "leave"
    assert cohorts.classify_text("Goodbye evac") == "leave"
    assert cohorts.classify_text("evac left (joined 3 days ago)") is None
    assert cohorts.classify_text("cleft palate") is None  # word boundary: 'left' inside a word doesn't count


def test_pair_stints():
    t = 1_000_000.0
    # join, quick leave, rejoin, still here (matches current joined_at)
    assert cohorts.pair_stints([("join", t), ("leave", t + 120), ("join", t + 5000)], t + 5003) == [(t, t + 120), (t + 5000, None)]
    # joined, never logged a leave, gone now
    assert cohorts.pair_stints([("join", t)], None) == [(t, 0)]
    # two joins in a row: the first one ended at an unknown time
    assert cohorts.pair_stints([("join", t), ("join", t + 9000)], t + 9000) == [(t, 0), (t + 9000, None)]
    # a leave with no join in the window is ignored
    assert cohorts.pair_stints([("leave", t)], None) == []


def test_build_log_records_with_day_level_activity():
    start, end = local_ts(2026, 6, 1), local_ts(2026, 10, 1)
    a, b, c = 111111111111111111, 222222222222222222, 333333333333333333
    events = [
        (a, "join", local_ts(2026, 9, 10, 15)), (a, "leave", local_ts(2026, 9, 10, 15, 4)),
        (b, "join", local_ts(2026, 9, 12, 9)),
        (999, "join", local_ts(2026, 5, 1)),  # before the window
    ]
    members_now = {b: local_ts(2026, 9, 12, 9, 0, 3), c: local_ts(2026, 9, 20, 18)}
    day_counts = {"2026-09-10": {str(a): 2}, "2026-09-13": {str(b): 5}, "2026-09-21": {str(b): 1}}
    recs = cohorts.build_log_records(events, members_now, start_ts=start, end_ts=end, day_counts=day_counts,
                                     today=date(2026, 10, 3), data_from=date(2026, 7, 5))
    by_user = {r["u"]: r for r in recs}
    assert set(by_user) == {a, b, c}
    assert by_user[a]["l"] == local_ts(2026, 9, 10, 15, 4) and by_user[a]["d"] == [0]
    assert by_user[b]["l"] is None and by_user[b]["d"] == [1, 9] and by_user[b]["n"] == 6
    assert by_user[c]["o"] == "member" and by_user[c]["d"] == []
    old = cohorts.build_log_records([(a, "join", local_ts(2026, 6, 2))], {}, start_ts=start, end_ts=end,
                                    day_counts={}, today=date(2026, 10, 3), data_from=date(2026, 7, 5))
    assert old[0]["d"] is None  # message data for that period is gone: unknown, not "silent"


def _rec(join, leave=None, days=(), acct_age_days=400, ob=None, roles=None, source=None, origin="live"):
    r = cohorts.new_record(UID, join, source=source, origin=origin)
    r["a"] = join - acct_age_days * 86400
    r["l"], r["d"], r["ob"], r["r"] = leave, list(days) if days is not None else None, ob, roles
    return r


def test_summarize_funnel():
    now = local_ts(2026, 10, 3, 12)
    j = local_ts(2026, 8, 25, 12)  # 39 days ago: old enough for every metric
    recs = [
        _rec(j, leave=j + 120, days=[], acct_age_days=2, ob=False, roles=0),           # bounced in 2 min, silent, new acct
        _rec(j, leave=j + 3000, days=[0], ob=True, roles=3),                            # said hi, gone in 50 min
        _rec(j, leave=j + 10 * 86400, days=[0, 1]),                                      # stayed 10 days
        _rec(j, days=[0, 2, 8, 30]),                                                     # regular
        _rec(j, days=[]),                                                                # silent, still here
        _rec(j, days=None, origin="log"),                                                # activity unknown
        _rec(j, leave=0, days=[], origin="log"),                                          # left, time unknown
    ]
    s = cohorts.summarize(recs, now)
    assert s["joined"] == 7 and s["still_here"] == 3 and s["left"] == 4 and s["left_unknown_time"] == 1
    assert s["left_10m"] == 1 and s["left_1h"] == 2 and s["left_7d"] == 2
    assert s["spoke"] == 3 and s["spoke_known"] == 6
    assert (s["silent_leavers"], s["leavers_known"]) == (2, 4)
    assert s["active_week2"] == (1, 6) and s["active_day30"] == (1, 6)
    assert s["came_back"] == (2, 6)
    assert s["new_accounts_7d"] == 1
    q = s["quick"]
    assert q["count"] == 2 and q["new_account_30d"] == 1 and q["spoke"] == (1, 2)
    assert q["onboarding_done"] == (1, 2) and q["picked_roles"] == (1, 2)


def test_young_joins_are_not_counted_against_week2():
    now = local_ts(2026, 10, 3, 12)
    s = cohorts.summarize([_rec(local_ts(2026, 9, 30), days=[0])], now)
    assert s["active_week2"] == (0, 0)


def test_by_source_only_uses_live_records_and_weekly_cohorts():
    now = local_ts(2026, 10, 3, 12)
    recs = [_rec(local_ts(2026, 9, 22, 10), source="Disboard", days=[0]),
            _rec(local_ts(2026, 9, 23, 10), source="Disboard", leave=local_ts(2026, 9, 23, 10, 5), days=[]),
            _rec(local_ts(2026, 9, 29, 10), source="discord.me", days=[]),
            _rec(local_ts(2026, 9, 29, 10), source=None, origin="log", days=[])]
    names = dict(cohorts.by_source(recs, now))
    assert names["Disboard"]["joined"] == 2 and names["discord.me"]["joined"] == 1 and "Unknown" not in names
    weeks = cohorts.weekly_cohorts(recs, now)
    assert [w["week"] for w in weeks] == ["2026-09-21", "2026-09-28"]
    assert weeks[0]["joined"] == 2 and weeks[0]["left_1h"] == 1


def test_anonymized_rows_have_no_user_ids():
    rows = cohorts.anonymized([_rec(local_ts(2026, 9, 1, 12), leave=local_ts(2026, 9, 1, 12, 3), days=[0])])
    assert "u" not in rows[0] and rows[0]["left_after_minutes"] == 3.0
    assert str(UID) not in str(rows)


def test_store_roundtrip_and_rebuild_never_touches_live(tmp_path):
    store = CohortStore(tmp_path, 42)
    store.load()
    since = store.ensure_tracking_since(local_ts(2026, 9, 25))
    live = cohorts.new_record(5, local_ts(2026, 9, 28), source="Disboard")
    store.add(live)
    store.replace_rebuilt([_rec(local_ts(2026, 8, 3), origin="log"), _rec(local_ts(2026, 9, 26), origin="log")], since)
    assert len(store.records) == 2  # the 9/26 rebuilt one is after tracking began: dropped
    store.flush()
    again = CohortStore(tmp_path, 42)
    again.load()
    assert again.tracking_since == since and len(again.records) == 2
    assert again.open_record(5)["s"] == "Disboard"
    again.replace_rebuilt([], since)  # re-run with nothing: removes old log rows, keeps live
    assert [r["o"] for r in again.records.values()] == ["live"]
    again.flush()
    assert sorted(p.name for p in (tmp_path / "cohorts" / "42").glob("*.json")) == ["2026-09.json", "meta.json"]
