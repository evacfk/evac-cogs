"""The download -> shrink -> post -> clean-up flow against fakes. Proves ordering,
cursors, retries, caps and temp-file hygiene; it does NOT prove real Telegram or
Discord behaviour."""
import os

import pytest

from tgfeed import constants, pipeline
from tgfeed.models import MediaItem, TopicMapping, TopicStats
from tgfeed.source import SourceFlood

NOW = 2_000_000.0
LIMIT = 1000


class FakeSource:
    def __init__(self, sizes=None, fail_on=None, flood_on=None):
        self.sizes = sizes or {}
        self.fail_on, self.flood_on = fail_on or set(), flood_on or set()
        self.downloads = []

    async def download_bytes(self, item):
        self._check(item)
        return b"p" * self.sizes.get(item.msg_id, 100)

    async def download_file(self, item, path):
        self._check(item)
        with open(path, "wb") as f:
            f.write(b"v" * self.sizes.get(item.msg_id, 100))
        return path

    def _check(self, item):
        self.downloads.append(item.msg_id)
        if item.msg_id in self.flood_on:
            raise SourceFlood(60)
        if item.msg_id in self.fail_on:
            raise RuntimeError("download blew up")


class Harness:
    def __init__(self, tmp_path, source=None, slots=1000, shrink_to=None, send_fails=0):
        self.tmp = str(tmp_path)
        self.source = source or FakeSource()
        self.sent, self.sleeps, self.slots = [], [], slots
        self.shrink_to, self.send_fails, self.shrinks = shrink_to, send_fails, []
        self.seen_paths = []

    def deps(self):
        async def sleep(s):
            self.sleeps.append(s)

        def reserve(n):
            if self.slots < n:
                return False
            self.slots -= n
            return True

        async def send_batch(topic_id, files):
            if self.send_fails:
                self.send_fails -= 1
                raise RuntimeError("discord said no")
            self.seen_paths += [f.path for f in files if f.path]
            self.sent.append((topic_id, [(f.name, f.size, f.data is not None) for f in files]))
            return 5000 + len(self.sent)

        async def shrink(src, dst, item, limit):
            self.shrinks.append((src, dst))
            if self.shrink_to is None:
                return None
            with open(dst, "wb") as f:
                f.write(b"s" * self.shrink_to)
            return self.shrink_to

        return pipeline.Deps(
            source=self.source, group=None, limit_bytes=LIMIT, tmpdir=self.tmp, gap=(5, 12),
            rng=__import__("random").Random(1), sleep=sleep, now=lambda: NOW, reserve=reserve,
            send_batch=send_batch, shrink=shrink, fails={},
        )

    def leftovers(self):
        return os.listdir(self.tmp)


def photo(mid, gid=None, date=NOW - 1000, topic=7):
    return MediaItem(msg_id=mid, topic_id=topic, kind="photo", grouped_id=gid, date=date, ext=".jpg")


def video(mid, size, ext=".mp4", gid=None, date=NOW - 1000, topic=7):
    return MediaItem(msg_id=mid, topic_id=topic, kind="video", grouped_id=gid, date=date, size=size, ext=ext, duration=10, height=720)


def mapping(cursor=0):
    return TopicMapping(topic_id=7, title="t", channel_id=1, cursor=cursor)


async def run(h, items, scan_max=None, cursor=0, m=None):
    m = m or mapping(cursor)
    stats = TopicStats()
    deps = h.deps()
    stop = await pipeline.process_topic(m, items, scan_max if scan_max is not None else max([i.msg_id for i in items] + [cursor]), deps, stats)
    return m, stats, stop, deps


async def test_single_photo_posted_and_cursor_advances(tmp_path):
    h = Harness(tmp_path)
    m, stats, stop, _ = await run(h, [photo(11)], scan_max=15)
    assert h.sent == [(7, [("file-1.jpg", 100, True)])]
    assert h.source.downloads == [11]
    assert m.cursor == 15 and stats.posted_files == 1 and stop is None


async def test_album_stays_together_in_one_message(tmp_path):
    h = Harness(tmp_path)
    await run(h, [photo(11, gid=1), photo(12, gid=1), photo(13, gid=1)])
    assert len(h.sent) == 1 and len(h.sent[0][1]) == 3
    assert h.source.downloads == [11, 12, 13]


async def test_big_album_splits_by_size_but_keeps_order(tmp_path):
    h = Harness(tmp_path, FakeSource({11: 600, 12: 600, 13: 600}))
    await run(h, [photo(11, gid=1), photo(12, gid=1), photo(13, gid=1)])
    assert [len(files) for _, files in h.sent] == [1, 1, 1]
    assert h.source.downloads == [11, 12, 13]


async def test_filenames_are_neutral(tmp_path):
    h = Harness(tmp_path)
    await run(h, [photo(11, gid=1), video(12, 500, gid=1)])
    names = [n for _, files in h.sent for n, *_ in files]
    assert names == ["file-1.jpg", "file-2.mp4"]


async def test_oversize_photo_skipped_but_unit_completes(tmp_path):
    h = Harness(tmp_path, FakeSource({11: 5000}))
    m, stats, _, _ = await run(h, [photo(11)], scan_max=11)
    assert h.sent == [] and stats.skipped_oversize == 1 and m.cursor == 11


async def test_video_within_limit_is_posted_untouched_from_disk(tmp_path):
    h = Harness(tmp_path, FakeSource({20: 400}))
    await run(h, [video(20, 400)])
    assert h.sent == [(7, [("file-1.mp4", 400, False)])] and h.shrinks == []


async def test_video_over_limit_is_shrunk(tmp_path):
    h = Harness(tmp_path, FakeSource({20: 5000}), shrink_to=800)
    _, stats, _, _ = await run(h, [video(20, 5000)])
    assert h.sent == [(7, [("file-1.mp4", 800, False)])] and stats.shrunk == 1


async def test_unshrinkable_video_is_skipped_not_retried(tmp_path):
    h = Harness(tmp_path, FakeSource({20: 5000}), shrink_to=None)
    m, stats, stop, _ = await run(h, [video(20, 5000)])
    assert h.sent == [] and stats.skipped_too_long == 1 and m.cursor == 20 and stop is None


async def test_unplayable_container_is_converted_even_when_small(tmp_path):
    h = Harness(tmp_path, FakeSource({20: 300}), shrink_to=250)
    await run(h, [video(20, 300, ext=".mkv")])
    assert len(h.shrinks) == 1 and h.sent[0][1][0][0] == "file-1.mp4"


async def test_source_video_too_big_to_even_download(tmp_path):
    h = Harness(tmp_path)
    _, stats, _, _ = await run(h, [video(20, constants.MAX_SOURCE_VIDEO_BYTES + 1)])
    assert h.source.downloads == [] and stats.skipped_oversize == 1


async def test_temp_files_deleted_after_success(tmp_path):
    h = Harness(tmp_path, FakeSource({20: 5000}), shrink_to=800)
    await run(h, [video(20, 5000)])
    assert h.seen_paths and h.leftovers() == []


async def test_temp_files_deleted_when_discord_send_fails(tmp_path):
    h = Harness(tmp_path, FakeSource({20: 400}), send_fails=1)
    m, stats, stop, _ = await run(h, [video(20, 400)])
    assert h.leftovers() == [] and stop == "error" and stats.errors == 1
    assert m.cursor == 19          # parked just before the failed unit, so message 20 is retried, not skipped


async def test_temp_files_deleted_when_a_later_download_fails(tmp_path):
    h = Harness(tmp_path, FakeSource({20: 400, 21: 400}, fail_on={21}))
    await run(h, [video(20, 400, gid=1), video(21, 400, gid=1)])
    assert h.leftovers() == [] and h.sent == []


async def test_failed_unit_blocks_later_ones_to_keep_order_and_cursor_stays_put(tmp_path):
    h = Harness(tmp_path, FakeSource(fail_on={11}))
    m, _, stop, _ = await run(h, [photo(11), photo(12)], cursor=5, scan_max=12)
    assert stop == "error" and h.sent == [] and m.cursor == 10 and m.last_error


async def test_poisoned_unit_is_skipped_after_three_tries(tmp_path):
    h = Harness(tmp_path, FakeSource(fail_on={11}))
    deps = h.deps()
    m = mapping()
    for attempt in range(constants.MAX_UNIT_RETRIES):
        await pipeline.process_topic(m, [photo(11), photo(12)], 12, deps, TopicStats())
    # third failure skips 11 and carries on to 12 in the same pass
    assert h.sent == [(7, [("file-1.jpg", 100, True)])] and m.cursor == 12
    assert h.source.downloads.count(11) == constants.MAX_UNIT_RETRIES and h.source.downloads[-1] == 12


async def test_fresh_album_is_held_back_and_cursor_stops_before_it(tmp_path):
    h = Harness(tmp_path)
    fresh = [photo(21, gid=9, date=NOW - 3), photo(22, gid=9, date=NOW - 2)]
    m, _, stop, _ = await run(h, [photo(20)] + fresh, cursor=10, scan_max=22)
    assert h.source.downloads == [20] and len(h.sent) == 1 and m.cursor == 20 and stop is None
    # next cycle, album now settled
    h2 = Harness(tmp_path)
    old = [photo(21, gid=9, date=NOW - 100), photo(22, gid=9, date=NOW - 90)]
    m2, *_ = await run(h2, old, cursor=m.cursor, scan_max=22, m=m)
    assert h2.source.downloads == [21, 22] and len(h2.sent) == 1 and len(h2.sent[0][1]) == 2 and m.cursor == 22


async def test_items_at_or_below_cursor_are_never_reposted(tmp_path):
    h = Harness(tmp_path)
    await run(h, [photo(10), photo(11)], cursor=10, scan_max=11)
    assert h.source.downloads == [11]


async def test_rate_cap_stops_cleanly_and_resumes_later(tmp_path):
    h = Harness(tmp_path, slots=1)
    m, stats, stop, _ = await run(h, [photo(11), photo(12)], scan_max=12)
    assert stop == "rate_cap" and h.source.downloads == [11] and m.cursor == 11
    h.slots = 10
    await run(h, [photo(11), photo(12)], scan_max=12, m=m)
    assert h.source.downloads == [11, 12] and len(h.sent) == 2 and m.cursor == 12


async def test_downloads_are_spaced_with_random_gaps(tmp_path):
    h = Harness(tmp_path)
    await run(h, [photo(11, gid=1), photo(12, gid=1), photo(13, gid=1)])
    assert len(h.sleeps) == 2 and all(5 <= s <= 12 for s in h.sleeps)


async def test_flood_propagates_but_keeps_progress(tmp_path):
    h = Harness(tmp_path, FakeSource(flood_on={12}))
    m = mapping()
    with pytest.raises(SourceFlood):
        await pipeline.process_topic(m, [photo(11), photo(12)], 12, h.deps(), TopicStats())
    assert m.cursor == 11 and len(h.sent) == 1


async def test_nothing_new_still_advances_past_text_only_messages(tmp_path):
    h = Harness(tmp_path)
    m, *_ = await run(h, [], cursor=5, scan_max=30)
    assert m.cursor == 30 and h.sent == []
