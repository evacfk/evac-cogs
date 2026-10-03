from datetime import date, timedelta

import pytest

from serverpulse import embeds, engine, models
from serverpulse._testdata import local_ts, traffic

START = date(2026, 9, 1)


@pytest.fixture(scope="module")
def data():
    docs, cov, end = traffic(START, 28, seed=5)
    slots = engine.build_slots(docs, START, date(2026, 9, 28), end, cov)
    return docs, cov, end, slots


def report(data, kind="week", start=date(2026, 9, 21), end=date(2026, 9, 27), ps=date(2026, 9, 14), pe=date(2026, 9, 20)):
    docs, cov, now, slots = data
    r = engine.build_period_report(docs, kind, start, end, "label", ps, pe, now, cov)
    r.anomalies = engine.find_anomalies(slots, start, end)
    return r


def assert_fits(embed):
    assert len(embed.description or "") <= 4096
    for f in embed.fields:
        assert len(f.value) <= 1024, f.name
        assert f.name and f.value


def field_names(embed):
    return [f.name for f in embed.fields]


class TestFormatting:
    def test_hour_labels(self):
        assert [embeds.fmt_hour(h) for h in (0, 1, 11, 12, 13, 23)] == ["12am", "1am", "11am", "12pm", "1pm", "11pm"]

    def test_hour_ranges_compress_the_meridiem(self):
        assert embeds.fmt_hour_range(20, 2) == "8–10pm"
        assert embeds.fmt_hour_range(23, 2) == "11pm–1am"
        assert embeds.fmt_hour_range(9, 1) == "9–10am"
        assert embeds.fmt_hour_range(11, 2) == "11am–1pm"

    def test_hour_blocks_merge_consecutive_hours(self):
        assert embeds.hour_blocks([21, 22, 23]) == ["9pm–12am"]
        assert embeds.hour_blocks([5, 21, 22]) == ["5–6am", "9–11pm"]

    def test_durations(self):
        assert embeds.fmt_duration(5 * 3600 + 12 * 60) == "5h 12m"
        assert embeds.fmt_duration(3 * 3600) == "3h"
        assert embeds.fmt_duration(42 * 60) == "42m"

    def test_clock(self):
        assert embeds.fmt_clock(local_ts(2026, 9, 30, 14, 5)) == "Wed 2:05pm"
        assert embeds.fmt_clock(local_ts(2026, 9, 30, 0, 30)) == "Wed 12:30am"

    def test_arrow(self):
        assert embeds.arrow(12.4) == "▲ 12%" and embeds.arrow(-8) == "▼ 8%" and embeds.arrow(0.2) == "≈ flat" and embeds.arrow(None) == ""

    def test_sparkline_scales_to_the_max(self):
        s = embeds.sparkline([0, 5, 10])
        assert s[0] == "▁" and s[-1] == "█" and len(s) == 3
        assert embeds.sparkline([0, 0]) == "▁▁"

    def test_clip(self):
        assert len(embeds.clip("x" * 2000)) == 1024


class TestSentence:
    def test_names_busiest_and_quietest_blocks(self, data):
        text = embeds.busy_quiet_sentence(engine.hour_table(data[3]))
        assert "busiest" in text and "quietest" in text and ("pm" in text)

    def test_none_without_enough_hours(self):
        assert embeds.busy_quiet_sentence(engine.hour_table([])) is None


class TestPeriodEmbeds:
    def test_week_report_has_the_expected_sections(self, data):
        e = embeds.period_embed(report(data))
        assert_fits(e)
        names = field_names(e)
        for want in ("🔥 Busiest hours", "😴 Quietest hours", "By day", "⚡ Peak moment", "🕳️ Longest dead stretch", "💬 Top channels"):
            assert want in names, want
        assert "messages" in e.description and "▲" in e.description or "▼" in e.description or "≈" in e.description

    def test_month_report_breaks_down_by_week(self, data):
        e = embeds.period_embed(report(data, "month", date(2026, 9, 1), date(2026, 9, 30), date(2026, 8, 1), date(2026, 8, 31)))
        assert_fits(e)
        assert "By week" in field_names(e)

    def test_day_report_has_hour_by_hour_line(self, data):
        e = embeds.period_embed(report(data, "day", date(2026, 9, 20), date(2026, 9, 20), date(2026, 9, 19), date(2026, 9, 19)))
        assert_fits(e)
        assert "Hour by hour" in field_names(e)

    def test_digest_variant_titles_and_lists_best_windows(self, data):
        r = report(data)
        best = engine.find_windows(data[3], 2, 3)
        e = embeds.period_embed(r, digest=True, best=best)
        assert "digest" in e.title and any("Best windows" in n for n in field_names(e))
        assert_fits(e)

    def test_in_progress_period_says_so(self, data):
        docs, cov, now, slots = data
        r = engine.build_period_report(docs, "week", date(2026, 9, 28), date(2026, 10, 4), "w", date(2026, 9, 21), date(2026, 9, 27), now, cov)
        assert "(so far)" in embeds.period_embed(r).description

    def test_empty_period_is_friendly(self):
        r = engine.build_period_report({}, "day", date(2026, 9, 14), date(2026, 9, 14), "d", date(2026, 9, 13), date(2026, 9, 13), local_ts(2026, 9, 30), None)
        e = embeds.period_embed(r)
        assert "No data" in e.description

    def test_partial_start_is_disclosed(self, data):
        docs, cov, now, slots = data
        r = engine.build_period_report(docs, "week", date(2026, 8, 31), date(2026, 9, 6), "w", date(2026, 8, 24), date(2026, 8, 30), now, cov)
        assert r.partial_start and "partway" in embeds.period_embed(r).description


class TestOtherEmbeds:
    def test_hours_table_has_24_rows_and_legend(self, data):
        e = embeds.hours_embed(engine.hour_table(data[3]), None, "last 28 days")
        assert_fits(e)
        table = e.fields[0].value
        assert table.count("\n") >= 25 and "12am" in table and "11pm" in table and "▲" in table
        assert any(f.name == "Legend" for f in e.fields)

    def test_hours_table_for_one_weekday(self, data):
        e = embeds.hours_embed(engine.hour_table(data[3], 4), 4, "x")
        assert "Fridays" in e.title

    def test_hour_focus_explains_rank_and_gap(self, data):
        e = embeds.hour_focus_embed(engine.hour_focus(data[3], 20), "last 28 days")
        assert_fits(e)
        assert "ranks **#" in e.description and "8–9pm" in e.title
        assert any("by weekday" in f.name for f in e.fields)

    def test_hour_focus_for_the_busiest_hour_says_so(self, data):
        rows = engine.hour_table(data[3])
        top = min((r for r in rows if r.n), key=lambda r: r.rank).hour
        e = embeds.hour_focus_embed(engine.hour_focus(data[3], top), "x")
        assert "busiest hour" in e.description

    def test_hour_focus_without_data(self):
        e = embeds.hour_focus_embed(engine.hour_focus([], 5), "x")
        assert "No data" in e.description

    def test_windows(self, data):
        best, quiet = engine.find_windows(data[3], 2, 3), engine.find_windows(data[3], 2, 3, "quiet")
        e = embeds.windows_embed(best, quiet, 2, None, "x")
        assert_fits(e)
        assert len(e.fields) == 2
        assert "Not enough data" in embeds.windows_embed([], [], 2, None, "x").fields[0].name

    def test_channels_and_top_and_anomalies_and_members(self, data):
        ch = engine.channel_ranking(data[0], START, date(2026, 9, 28))
        assert "<#" in embeds.channels_embed(ch, "x").description
        assert "No messages" in embeds.channels_embed([], "x").description
        assert "1." in embeds.top_chatters_embed([("Ann", 5)], "x").description
        assert "90 days" in embeds.top_chatters_embed([], "x").description
        assert "Nothing unusual" in embeds.anomalies_embed([], "x").description
        a = engine.Anomaly("day", "busy", "2026-09-20", None, 5, 300, 100, 3.0)
        assert "3.0× busier" in embeds.anomalies_embed([a], "x").description
        assert "Total" in embeds.members_embed([("2026-09-20", 3, 1)], "x").description
        assert "No join/leave" in embeds.members_embed([], "x").description

    def test_collecting_embed_variants(self):
        now = local_ts(2026, 9, 30, 12)
        assert "hasn't started" in embeds.collecting_embed(None, now).description
        assert "first full hour" in embeds.collecting_embed(now, now + 600).description
        assert "backfill" in embeds.collecting_embed(now, now + 600).description

    def test_live_board_and_overview(self, data):
        docs, cov, now, slots = data
        live = embeds.LiveView(
            now_ts=now, last_msg_age_s=45, hour_msgs=120, hour_chatters=30, concurrency=8, usual_hour_msgs=100.0, pace_ratio=1.6,
            today_msgs=3000, today_chatters=90, today_vs_usual=(3000, 2000.0, 1.5, 6), joins_today=3, leaves_today=1,
            next_best=engine.upcoming_window(slots, now, 2, 24, "best"), next_quiet=engine.upcoming_window(slots, now, 2, 24, "quiet"),
            hours=engine.hour_table(slots), history_days=56,
        )
        board = embeds.board_embed(live)
        assert_fits(board)
        assert "Right now" in field_names(board) and "Updated" in field_names(board)
        assert "🔥 1.6× usual" in board.fields[0].value
        over = embeds.overview_embed(live, "sentence", live.hours, 28)
        assert_fits(over)
        assert "More" in field_names(over)

    def test_board_handles_a_brand_new_server(self):
        live = embeds.LiveView(now_ts=local_ts(2026, 9, 30, 12), last_msg_age_s=None, hour_msgs=0, hour_chatters=0, concurrency=0,
                               usual_hour_msgs=None, pace_ratio=None, today_msgs=0, today_chatters=None, today_vs_usual=None,
                               joins_today=0, leaves_today=0, next_best=None, next_quiet=None, hours=engine.hour_table([]), history_days=56)
        e = embeds.board_embed(live)
        assert "no messages yet" in e.fields[0].value
        assert "not enough data" in e.fields[2].value

    def test_admin_embeds(self):
        e = embeds.settings_embed(ignored=[1, 2], mod_channel=3, mod_role=4, exclude_commands=True, coverage_start_ts=local_ts(2026, 9, 1),
                                  live_since_ts=None, board={"channel_id": 3, "message_id": 9}, digest_weekly={"enabled": True},
                                  digest_monthly={"enabled": False}, days_stored=12)
        assert_fits(e)
        assert "<#3>" in e.fields[0].value
        v = embeds.version_embed(days_stored=1, coverage_start_ts=None, live_since_ts=None, pending_msgs=3, backfill_status="none")
        assert "1.2.1" in v.description

    def test_backfill_embed_states(self):
        now = local_ts(2026, 9, 30)
        assert "No backfill" in embeds.backfill_embed({"status": "none"}, now).description
        running = embeds.backfill_embed({"status": "running", "days": 30, "channels_total": 10, "channels_done": 4, "messages": 12345, "current": "#general", "started": now}, now)
        assert "Now reading" in field_names(running) and "12,345" in [f.value for f in running.fields]
        err = embeds.backfill_embed({"status": "error", "days": 30, "error": "boom", "skipped": ["#secret"]}, now)
        assert "Resume" in field_names(err) and "Skipped (no access)" in field_names(err)


class TestDayChart:
    def test_bars_and_axis_are_the_same_width_so_labels_line_up(self):
        chart = embeds.day_chart(list(range(24)))
        _, bars, axis, _ = chart.split("\n")[0], *chart.split("\n")[1:4]
        assert len(bars) == 48
        assert len(axis) <= 48
        assert axis.index("12p") == 24 and axis.index("9p") == 42
