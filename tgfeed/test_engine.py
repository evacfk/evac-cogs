import random

import pytest

from tgfeed import constants, engine
from tgfeed.models import MediaItem, TopicInfo

NOW = 1_000_000.0


def item(mid, gid=None, date=0.0, topic=5, kind="photo", size=0):
    return MediaItem(msg_id=mid, topic_id=topic, kind=kind, grouped_id=gid, date=date, size=size)


class TestUnits:
    def test_sorted_and_albums_folded(self):
        units = engine.group_units([item(4), item(2, gid=9), item(3, gid=9), item(1)])
        assert [[i.msg_id for i in u] for u in units] == [[1], [2, 3], [4]]

    def test_different_albums_not_merged(self):
        units = engine.group_units([item(1, gid=1), item(2, gid=2)])
        assert len(units) == 2

    def test_singles_are_ready_at_once(self):
        assert engine.unit_is_ready([item(1, date=NOW)], NOW)

    def test_fresh_album_waits_and_old_album_goes(self):
        fresh = [item(1, gid=1, date=NOW - 5), item(2, gid=1, date=NOW - 3)]
        old = [item(1, gid=1, date=NOW - 100), item(2, gid=1, date=NOW - 90)]
        assert not engine.unit_is_ready(fresh, NOW, settle=45)
        assert engine.unit_is_ready(old, NOW, settle=45)

    def test_split_ready_keeps_order_and_holds_everything_after_a_waiting_album(self):
        units = [[item(1)], [item(2, gid=1, date=NOW)], [item(3)]]
        ready, pending = engine.split_ready(units, NOW)
        assert [u[0].msg_id for u in ready] == [1]
        assert [u[0].msg_id for u in pending] == [2, 3]

    def test_split_ready_caps_units_per_cycle(self):
        units = [[item(i)] for i in range(1, 8)]
        ready, pending = engine.split_ready(units, NOW, max_units=3)
        assert len(ready) == 3 and len(pending) == 4


class TestCursor:
    def test_nothing_pending_jumps_to_scan_max(self):
        assert engine.next_cursor(10, 15, None, 40) == 40

    def test_pending_stops_just_before_it(self):
        assert engine.next_cursor(10, 15, 20, 40) == 19

    def test_never_goes_backwards(self):
        assert engine.next_cursor(50, 15, 20, 40) == 50
        assert engine.next_cursor(50, None, 20, 40) == 50

    def test_done_unit_alone(self):
        assert engine.next_cursor(10, 15, 16, 40) == 15


class TestPack:
    def test_ten_files_per_message(self):
        assert engine.pack_batches([1] * 12, 100, 10) == [list(range(10)), [10, 11]]

    def test_byte_limit_splits_in_order(self):
        assert engine.pack_batches([6, 6, 6], 10) == [[0], [1], [2]]
        assert engine.pack_batches([4, 4, 4], 10) == [[0, 1], [2]]

    def test_oversize_file_gets_no_batch(self):
        assert engine.pack_batches([5, 99, 5], 10) == [[0, 2]]

    def test_empty(self):
        assert engine.pack_batches([], 10) == []

    def test_upload_limit_keeps_a_margin_and_caps(self):
        assert engine.upload_limit(10 * 1024 * 1024) == 10 * 1024 * 1024 - constants.UPLOAD_MARGIN_BYTES
        assert engine.upload_limit(None) == constants.DEFAULT_GUILD_UPLOAD_BYTES - constants.UPLOAD_MARGIN_BYTES
        assert engine.upload_limit(999 * 1024 * 1024) == constants.HARD_MAX_UPLOAD_BYTES - constants.UPLOAD_MARGIN_BYTES


class TestRate:
    def test_slots_respect_both_caps(self):
        stamps = [NOW - 10] * 5 + [NOW - 7200] * 3
        assert engine.slots_available(stamps, NOW, per_hour=10, per_day=100) == 5
        assert engine.slots_available(stamps, NOW, per_hour=10, per_day=9) == 1
        assert engine.slots_available(stamps, NOW, per_hour=5, per_day=100) == 0

    def test_old_stamps_expire(self):
        assert engine.prune_rate_log([NOW - 90000, NOW - 10], NOW) == [NOW - 10]

    def test_wait_until_a_slot_frees(self):
        stamps = [NOW - 3000] * 10
        assert engine.seconds_until_slot(stamps, NOW, per_hour=10, per_day=500) == pytest.approx(600)
        assert engine.seconds_until_slot([], NOW, 10, 500) == 0

    def test_can_start_unit(self):
        assert engine.can_start_unit(10, 10, 60)
        assert not engine.can_start_unit(9, 10, 60)
        assert engine.can_start_unit(10, 15, 10)   # an album bigger than the hourly cap can still start


class TestFlood:
    def test_sleep_has_margin(self):
        assert engine.flood_sleep_seconds(100) == 100 + constants.FLOOD_MARGIN_SECONDS

    def test_third_flood_in_a_day_pauses(self):
        assert not engine.should_pause_after_flood([NOW - 100, NOW], NOW)
        assert engine.should_pause_after_flood([NOW - 200, NOW - 100, NOW], NOW)

    def test_old_floods_dont_count(self):
        assert not engine.should_pause_after_flood([NOW - 200000, NOW - 190000, NOW], NOW)


class TestTiming:
    def test_jitter_stays_in_band(self):
        rng = random.Random(1)
        for _ in range(200):
            assert 225 <= engine.jittered(300, rng) <= 375

    def test_gap_never_below_floor(self):
        rng = random.Random(1)
        for _ in range(100):
            assert engine.random_gap(0, 0, rng) >= constants.MIN_FILE_GAP_SECONDS
            assert 5 <= engine.random_gap(5, 12, rng) <= 12


class TestNamesAndTopics:
    def test_slug_basic_and_prefix(self):
        assert engine.slugify_channel_name("Cute Pets & Friends!", "tg-") == "tg-cute-pets-friends"

    def test_slash_and_pipe_become_word_breaks(self):
        assert engine.slugify_channel_name("Cats/Dogs") == "cats-dogs"
        assert engine.slugify_channel_name("a \\ b | c + d") == "a-b-c-d"
        assert engine.slugify_channel_name("Photos / Videos") == "photos-videos"

    def test_slug_unicode_kept(self):
        assert engine.slugify_channel_name("Café  Pics") == "café-pics"

    def test_slug_empty_falls_back_to_id(self):
        assert engine.slugify_channel_name("!!!", topic_id=77) == "topic-77"

    def test_slug_length_capped(self):
        assert len(engine.slugify_channel_name("a" * 300)) <= constants.CHANNEL_NAME_MAX

    TOPICS = [TopicInfo(1, "General"), TopicInfo(7, "Cats"), TopicInfo(8, "cats")]

    def test_resolve_by_id(self):
        assert engine.resolve_topic("7", self.TOPICS)[0].title == "Cats"

    def test_resolve_by_exact_title(self):
        assert engine.resolve_topic("general", self.TOPICS)[0].id == 1

    def test_ambiguous_title_refused(self):
        topic, reason = engine.resolve_topic("CATS", self.TOPICS)
        assert topic is None and "id" in reason

    def test_unknown(self):
        assert engine.resolve_topic("99", self.TOPICS)[0] is None
        assert engine.resolve_topic("nope", self.TOPICS)[0] is None
        assert engine.resolve_topic("", self.TOPICS)[0] is None


def test_chunk_lines_respects_limit():
    lines = ["x" * 100] * 50
    chunks = engine.chunk_lines(lines, limit=1000)
    assert all(len(c) <= 1000 for c in chunks) and sum(c.count("x" * 100) for c in chunks) == 50
