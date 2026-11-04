"""Pure maths behind `.wld economy`: no discord, no redbot."""
from wonderland import economy

BALANCES = [0, 0, 5, 100, 100, 50_000, 2_000_000]


def test_percentile_uses_nearest_rank():
    assert economy.percentile([], 50) == 0
    assert economy.percentile([1, 2, 3, 4], 50) == 2
    assert economy.percentile([1, 2, 3, 4], 100) == 4
    assert economy.percentile([1, 2, 3, 4], 1) == 1


def test_summary_looks_at_holders_for_spread_and_everyone_for_bands():
    s = economy.summarize(BALANCES)
    assert s["accounts"] == 7 and s["holders"] == 5
    assert s["total"] == 2_050_205 and s["mean"] == 410_041
    assert (s["median"], s["p90"], s["p99"], s["max"]) == (100, 2_000_000, 2_000_000, 2_000_000)
    assert s["top10_share"] == 1.0                      # fewer than ten holders: they all count
    assert s["top1_n"] == 1 and round(s["top1pct_share"], 4) == 0.9755
    bands = {b["label"]: (b["count"], b["coins"]) for b in s["bands"]}
    assert bands["0"] == (2, 0)
    assert bands["1 to 9,999"] == (3, 205)
    assert bands["10,000 to 99,999"] == (1, 50_000)
    assert bands["100,000 to 999,999"] == (0, 0)
    assert bands["1M to 9.99M"] == (1, 2_000_000)
    assert bands["10M and up"] == (0, 0)


def test_every_account_lands_in_exactly_one_band():
    s = economy.summarize(BALANCES + [999_999, 1_000_000, 9_999_999, 10_000_000])
    assert sum(b["count"] for b in s["bands"]) == s["accounts"]
    assert sum(b["coins"] for b in s["bands"]) == s["total"]


def test_negative_balances_count_as_nothing():
    s = economy.summarize([-500, 10])
    assert s["accounts"] == 2 and s["holders"] == 1 and s["total"] == 10


def test_empty_server_is_all_zeros_not_a_crash():
    s = economy.summarize([])
    assert s["accounts"] == s["holders"] == s["total"] == s["median"] == 0
    assert s["top10_share"] == 0.0 and s["top1_n"] == 0 and s["top1pct_share"] == 0.0


def test_top_ten_share_is_a_share_of_the_total():
    balances = [1_000] * 20 + [10_000] * 10
    s = economy.summarize(balances)
    assert round(s["top10_share"], 4) == round(100_000 / 120_000, 4)


def test_top_holders_sorted_biggest_first_ties_by_id_and_zeros_left_out():
    assert economy.top_holders({1: 50, 2: 50, 3: 10, 4: 0}, 3) == [(1, 50), (2, 50), (3, 10)]
    assert economy.top_holders({1: 50, 2: 50, 3: 10, 4: 0}, 2) == [(1, 50), (2, 50)]


def test_snapshot_keeps_only_aggregates():
    snap = economy.snapshot(economy.summarize(BALANCES), "2026-11-04")
    assert set(snap) == {"date", "total", "holders", "median", "p90", "top10_share"}


def test_a_second_run_the_same_day_replaces_the_snapshot():
    history = [{"date": "2026-11-03", "total": 1}]
    history = economy.add_snapshot(history, {"date": "2026-11-04", "total": 2})
    history = economy.add_snapshot(history, {"date": "2026-11-04", "total": 3})
    assert [s["date"] for s in history] == ["2026-11-03", "2026-11-04"]
    assert history[-1]["total"] == 3


def test_history_keeps_only_the_newest_snapshots():
    history = []
    for day in range(1, 61):
        history = economy.add_snapshot(history, {"date": f"2026-{(day - 1) // 28 + 1:02d}-{(day - 1) % 28 + 1:02d}", "total": day})
    assert len(history) == economy.MAX_SNAPSHOTS
    assert history[-1]["total"] == 60 and history[0]["total"] == 60 - economy.MAX_SNAPSHOTS + 1


def test_previous_snapshot_is_the_latest_one_before_today():
    history = [{"date": "2026-10-01"}, {"date": "2026-10-08"}]
    assert economy.previous_snapshot(history, "2026-10-08")["date"] == "2026-10-01"
    assert economy.previous_snapshot(history, "2026-10-09")["date"] == "2026-10-08"
    assert economy.previous_snapshot(history, "2026-09-01") is None
    assert economy.previous_snapshot([], "2026-10-09") is None


def test_change_since_the_last_snapshot():
    prev = {"date": "2026-10-01", "total": 1_000, "holders": 10, "median": 0, "top10_share": 0.50}
    cur = {"date": "2026-10-08", "total": 1_500, "holders": 12, "median": 40, "top10_share": 0.60}
    d = economy.change(prev, cur)
    assert d["since"] == "2026-10-01"
    assert d["total"] == 500 and d["total_pct"] == 50.0
    assert d["holders"] == 2
    assert d["median"] == 40 and d["median_pct"] is None      # no percentage from a zero start
    assert round(d["top10_share"], 2) == 0.10
