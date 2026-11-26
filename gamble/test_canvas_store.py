"""Canvas rules and storage: prices, parsing, all-or-nothing painting, moderation, statistics."""
import sqlite3
from datetime import datetime

import pytest

from gamble import canvas_store as cs
from gamble.canvas_store import CanvasStore, PriceChanged, next_price


@pytest.fixture
def store(tmp_path):
    s = CanvasStore(str(tmp_path / "c.sqlite"))
    yield s
    s.close()


# ---- prices

def test_next_price_is_1_2x_rounded_to_the_nearest_100():
    assert [next_price(p) for p in (1000, 1200, 1400, 1700, 2000)] == [1200, 1400, 1700, 2000, 2400]
    assert next_price(2400) == 2900            # 2880 -> 2900
    assert next_price(1_000_000) == 1_200_000


def test_prices_only_ever_go_up():
    price = cs.BASE_PRICE
    for _ in range(60):
        nxt = next_price(price)
        assert nxt > price and nxt % 100 == 0
        price = nxt


# ---- parsing

@pytest.mark.parametrize("raw,expected", [("#FFB7C5", "#ffb7c5"), ("ffb7c5", "#ffb7c5"), ("#fb7", "#ffbb77"), (" abc ", "#aabbcc")])
def test_parse_color_accepts_hex(raw, expected):
    assert cs.parse_color(raw) == expected


@pytest.mark.parametrize("raw", ["", "red", "#12", "#1234567", "#ggg", "12345g"])
def test_parse_color_rejects_the_rest(raw):
    assert cs.parse_color(raw) is None


@pytest.mark.parametrize("raw,expected", [("12,7", (12, 7)), ("12 7", (12, 7)), ("(12, 7)", (12, 7)), (" 0 , 49 ", (0, 49))])
def test_parse_xy(raw, expected):
    assert cs.parse_xy(raw) == expected


@pytest.mark.parametrize("raw", ["", "12", "a,b", "1,2,3", "1.5,2"])
def test_parse_xy_rejects(raw):
    assert cs.parse_xy(raw) is None


def test_clean_text_is_one_line_with_no_control_characters():
    assert cs.clean_text("  hi\n\nthere\x00  you \t") == "hi there you"


def test_rect_cells_takes_corners_in_any_order():
    assert cs.rect_cells(2, 3, 0, 2) == cs.rect_cells(0, 2, 2, 3)
    assert len(cs.rect_cells(0, 0, 9, 9)) == 100


def test_short_price():
    assert [cs.short_price(n) for n in (1000, 9999, 12_000, 150_000, 1_200_000, 25_000_000)] == \
        ["1,000", "9,999", "12k", "150k", "1.2M", "25.0M".replace(".0", "")]


def test_last_weekly_boundary_is_the_most_recent_sunday_6pm_pacific():
    def ts(y, m, d, h, mi=0):
        return datetime(y, m, d, h, mi, tzinfo=cs.PACIFIC).timestamp()
    sunday_6pm = ts(2026, 11, 22, 18)           # Sun Nov 22 2026
    assert cs.last_weekly_boundary(ts(2026, 11, 22, 18, 0)) == sunday_6pm       # exactly on it
    assert cs.last_weekly_boundary(ts(2026, 11, 22, 17, 59)) == ts(2026, 11, 15, 18)   # a minute early: last week's
    assert cs.last_weekly_boundary(ts(2026, 11, 26, 1, 49)) == sunday_6pm       # Thursday
    assert cs.last_weekly_boundary(ts(2026, 11, 28, 23, 0)) == sunday_6pm       # Saturday night
    assert cs.last_weekly_boundary(ts(2026, 11, 29, 18, 1)) == ts(2026, 11, 29, 18)  # next Sunday, after 6 PM


# ---- painting

def test_a_blank_pixel_costs_1000_and_a_paint_makes_it_dearer(store):
    assert store.price_at(5, 5) == 1000 and store.get_pixel(5, 5) is None
    store.paint(1, [(5, 5)], "#ff0000", "hello", (1000,), now=100.0)
    px = store.get_pixel(5, 5)
    assert (px.color, px.owner, px.price, px.text, px.painted_at) == ("#ff0000", 1, 1200, "hello", 100.0)
    store.paint(2, [(5, 5)], "#00ff00", "", (1200,), now=200.0)
    px = store.get_pixel(5, 5)
    assert (px.owner, px.price, px.text) == (2, 1400, "")          # previous owner and text replaced


def test_a_stale_price_writes_nothing(store):
    store.paint(1, [(5, 5)], "#ff0000", "", (1000,))
    with pytest.raises(PriceChanged) as e:
        store.paint(2, [(5, 5), (6, 6)], "#00ff00", "", (1000, 1000))     # (5,5) is 1200 now
    assert e.value.prices == (1200, 1000)
    assert store.get_pixel(6, 6) is None and store.get_pixel(5, 5).owner == 1
    assert store.paint_actions() == 1


def test_an_area_prices_each_pixel_separately_and_each_rises_from_its_own_price(store):
    store.paint(1, [(0, 0)], "#111111", "", (1000,))
    store.paint(1, [(0, 0)], "#111111", "", (1200,))        # (0,0) now 1400
    cells = cs.rect_cells(0, 0, 1, 0)
    prices = store.quote(cells)
    assert prices == (1400, 1000) and sum(prices) == 2400
    store.paint(2, cells, "#222222", "x", prices)
    assert (store.price_at(0, 0), store.price_at(1, 0)) == (1700, 1200)


def test_paint_is_all_or_nothing_when_the_write_fails_midway(store, monkeypatch):
    cells = [(1, 1), (2, 2), (3, 3)]
    calls = {"n": 0}

    class Boom(sqlite3.OperationalError):
        pass

    class FlakyDb:
        """Delegates to the real connection but fails on the third pixel write."""
        def __init__(self, db): self.db = db
        def __getattr__(self, name): return getattr(self.db, name)
        def __enter__(self): return self.db.__enter__()
        def __exit__(self, *a): return self.db.__exit__(*a)
        def execute(self, sql, *args):
            if sql.startswith("INSERT INTO pixels"):
                calls["n"] += 1
                if calls["n"] == 3:
                    raise Boom("disk full")
            return self.db.execute(sql, *args)

    store.db = FlakyDb(store.db)
    with pytest.raises(Boom):
        store.paint(1, cells, "#abcdef", "", store.quote(cells))
    store.db = store.db.db
    assert store.painted_count() == 0 and store.paint_actions() == 0 and store.revision == 0


def test_text_is_cleaned_and_capped_at_100(store):
    store.paint(1, [(0, 0)], "#000000", "a\nb" + "x" * 200, (1000,))
    text = store.get_pixel(0, 0).text
    assert len(text) == 100 and text.startswith("a b")


def test_every_paint_bumps_the_revision_that_the_image_cache_uses(store):
    r0 = store.revision
    store.paint(1, [(0, 0)], "#000000", "", (1000,))
    assert store.revision == r0 + 1


# ---- moderation

def test_clear_text_keeps_the_pixel_and_owner(store):
    store.paint(1, [(0, 0)], "#000000", "rude", (1000,))
    assert store.clear_text(0, 0) is True and store.clear_text(0, 0) is False
    px = store.get_pixel(0, 0)
    assert px.text == "" and px.owner == 1 and px.price == 1200


def test_wipe_member_blanks_their_pixels_back_to_1000_and_leaves_others(store):
    store.paint(1, [(0, 0), (1, 0)], "#000000", "", (1000, 1000))
    store.paint(2, [(2, 0)], "#000000", "", (1000,))
    store.paint(3, [(0, 0)], "#111111", "", (1200,))                  # 3 took (0,0) from 1
    assert store.wipe_member(3) == 1
    assert store.price_at(0, 0) == 1000 and store.get_pixel(0, 0) is None
    assert store.get_pixel(1, 0).owner == 1 and store.get_pixel(2, 0).owner == 2
    assert store.wipe_member(99) == 0


def test_reset_blanks_everything_but_keeps_the_burn_record(store):
    store.paint(1, [(0, 0)], "#000000", "", (1000,))
    assert store.reset() == 1
    assert store.painted_count() == 0 and store.price_at(0, 0) == 1000
    assert store.burned() == 1000 and store.painters() == 1


def test_ban_and_unban(store):
    assert store.ban(7, 1) is True and store.ban(7, 1) is False
    assert store.is_banned(7) and store.banned_ids() == [7]
    assert store.unban(7) is True and store.unban(7) is False and not store.is_banned(7)


# ---- statistics

def seed(store):
    day = 86400.0
    t0 = datetime(2026, 11, 20, 12, tzinfo=cs.PACIFIC).timestamp()
    store.paint(1, [(0, 0)], "#000000", "", (1000,), now=t0)                       # 1000
    store.paint(2, [(0, 0)], "#000000", "", (1200,), now=t0 + 1)                   # 1200
    store.paint(2, cs.rect_cells(5, 5, 6, 5), "#000000", "", (1000, 1000), now=t0 + day)   # 2 x 1000, one action
    store.paint(3, [(9, 9)], "#000000", "", (1000,), now=t0 + 3 * day)             # 1000
    return t0


def test_spenders_all_time_and_windowed(store):
    t0 = seed(store)
    assert store.spenders(None, 10) == [(2, 3200, 3), (1, 1000, 1), (3, 1000, 1)]
    assert store.spenders(t0 + 86400 - 1, 10) == [(2, 2000, 2), (3, 1000, 1)]
    assert store.spenders(t0 + 86400 - 1, 1, until=t0 + 2 * 86400) == [(2, 2000, 2)]


def test_most_pixels_owned_counts_current_owners_only(store):
    seed(store)
    assert store.most_pixels(10) == [(2, 3), (3, 1)]            # user 1 lost (0,0) to user 2


def test_priciest_paint_is_the_single_most_expensive_pixel(store):
    t0 = seed(store)
    best = store.priciest_paint(t0 - 1)
    assert (best["user_id"], best["x"], best["y"], best["paid"]) == (2, 0, 0, 1200)
    assert store.priciest_paint(t0 + 10 * 86400) is None


def test_burn_and_painter_counts_and_actions(store):
    t0 = seed(store)
    assert store.burned() == 5200 and store.painters() == 3
    assert store.paint_actions() == 4                           # the area paint is ONE action
    assert store.burned(t0 + 86400 - 1, t0 + 2 * 86400) == 2000


def test_paints_per_day_uses_pacific_days_and_includes_zero_days(store):
    t0 = seed(store)
    now = t0 + 4 * 86400
    rows = store.paints_per_day(7, now)
    assert len(rows) == 7
    by_day = {d.isoformat(): (a, p) for d, a, p in rows}
    assert by_day["2026-11-20"] == (2, 2) and by_day["2026-11-21"] == (1, 2)
    assert by_day["2026-11-22"] == (0, 0) and by_day["2026-11-23"] == (1, 1)


def test_burned_per_week_buckets_are_rolling_and_oldest_first(store):
    t0 = seed(store)
    now = t0 + 10 * 86400
    weeks = store.burned_per_week(4, now)
    assert len(weeks) == 4
    assert [c for _, c in weeks] == [0, 0, 4200, 1000]     # the day-3 paint starts the newest window


def test_launch_time_is_recorded_once(tmp_path):
    path = str(tmp_path / "x.sqlite")
    a = CanvasStore(path)
    first = a.launched_at()
    a.close()
    assert CanvasStore(path).launched_at() == first
