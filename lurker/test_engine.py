"""Pure-logic tests for lurker.engine (no discord / redbot needed)."""
from lurker import engine

NOW = 1_800_000_000.0
DAY = engine.DAY


def test_cutoff_ts():
    assert engine.cutoff_ts(NOW, 30) == NOW - 30 * DAY


def test_resolve_last_active_prefers_tracked_then_join_then_zero():
    assert engine.resolve_last_active(500.0, 100.0) == 500.0
    assert engine.resolve_last_active(None, 100.0) == 100.0
    assert engine.resolve_last_active(None, None) == 0.0


def test_is_inactive_boundaries():
    cutoff = engine.cutoff_ts(NOW, 30)
    assert engine.is_inactive(NOW - 31 * DAY, None, cutoff)       # tracked, old
    assert not engine.is_inactive(NOW - 29 * DAY, None, cutoff)   # tracked, recent
    assert not engine.is_inactive(cutoff, None, cutoff)           # exactly at cutoff = still active
    assert not engine.is_inactive(None, NOW - 2 * DAY, cutoff)    # new join gets the grace window
    assert engine.is_inactive(None, NOW - 400 * DAY, cutoff)      # old join, never seen
    # no information at all is never grounds for flagging
    assert not engine.is_inactive(None, None, cutoff)
    # a recent tracked timestamp beats an ancient join date
    assert not engine.is_inactive(NOW - DAY, NOW - 900 * DAY, cutoff)


def test_merge_last_active_keeps_newest_and_never_lowers():
    existing = {1: 100.0, 2: 500.0}
    scanned = {1: 300.0, 2: 200.0, 3: 50.0}
    merged = engine.merge_last_active(existing, scanned)
    assert merged == {1: 300.0, 2: 500.0, 3: 50.0}
    assert existing == {1: 100.0, 2: 500.0}  # inputs untouched


def test_report_due():
    assert engine.report_due(NOW, NOW - 7 * DAY, 7)
    assert not engine.report_due(NOW, NOW - 7 * DAY + 1, 7)
    assert engine.report_due(NOW, 0, 7)  # never sent


def test_trim_events_keeps_newest():
    events = [{"i": i} for i in range(10)]
    kept, dropped = engine.trim_events(events, cap=4)
    assert [e["i"] for e in kept] == [6, 7, 8, 9]
    assert dropped == 6
    same, none = engine.trim_events(events, cap=50)
    assert same == events and none == 0


def _ev(uid, kind, src, name=None):
    return {"uid": uid, "name": name or f"user{uid}", "ts": NOW, "kind": kind, "src": src}


def test_summarize_buckets():
    events = [
        _ev(1, "flag", "sweep"), _ev(2, "flag", "sweep"), _ev(3, "flag", "manual"),
        _ev(4, "unflag", "post"), _ev(5, "unflag", "post"), _ev(6, "unflag", "mod"),
    ]
    s = engine.summarize(events, {"backfill": 7000, "undo": 2, "dropped": 5})
    assert [e["uid"] for e in s["sweep"]] == [1, 2]
    assert [e["uid"] for e in s["manual"]] == [3]
    assert s["restored_self"] == 2
    assert s["restored_mod"] == 1
    assert s["bulk_flagged"] == 7000 and s["bulk_restored"] == 2 and s["dropped"] == 5


def test_format_name_list_empty_and_fits_embed_field():
    assert engine.format_name_list([]) == "none"
    many = [_ev(i, "flag", "sweep", name="A very long display name " + str(i)) for i in range(500)]
    text = engine.format_name_list(many)
    assert len(text) <= 1024
    assert "more" in text.splitlines()[-1]


def test_format_name_list_small_list_is_complete():
    text = engine.format_name_list([_ev(1, "flag", "sweep", "Ann"), _ev(2, "flag", "sweep", "Bob")])
    assert "Ann" in text and "Bob" in text and "more" not in text


def test_events_to_csv_roundtrip():
    out = engine.events_to_csv([_ev(1, "flag", "sweep", 'Name, with "quotes"')])
    lines = out.strip().splitlines()
    assert lines[0] == "time_utc,action,source,user_id,name"
    assert '"Name, with ""quotes"""' in lines[1]


def test_candidates_to_csv_handles_never_seen():
    out = engine.candidates_to_csv([(1, "ann", NOW, "tracked activity"), (2, "bob", None, "join date")])
    assert "never seen" in out
    assert "ann" in out
