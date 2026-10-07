import pytest

from redditfeed import constants, engine
from redditfeed.models import MediaItem


# -- normalize_subreddit -------------------------------------------------------

class TestNormalizeSubreddit:
    def test_plain_name(self):
        assert engine.normalize_subreddit("feet") == "feet"

    def test_strips_r_slash_prefix(self):
        assert engine.normalize_subreddit("r/feet") == "feet"

    def test_strips_leading_slash_r_slash(self):
        assert engine.normalize_subreddit("/r/feet") == "feet"

    def test_lowercases_and_strips_whitespace(self):
        assert engine.normalize_subreddit("  FeetISH  ") == "feetish"


# -- dedup -------------------------------------------------------------------

class TestDedup:
    def test_build_dedup_key_combines_channel_and_post(self):
        assert engine.build_dedup_key(123, "abc") == "123:abc"

    def test_same_post_id_different_channels_are_different_keys(self):
        """A crosspost mapped under two subreddits into the SAME channel should
        dedup (one key), but the same post reaching a DIFFERENT channel must
        still be allowed to post there -- that's the whole point of keying by
        (channel, post) instead of post alone."""
        key_a = engine.build_dedup_key(1, "abc")
        key_b = engine.build_dedup_key(2, "abc")
        assert key_a != key_b

    def test_filter_unseen_drops_already_seen_posts_for_that_channel(self):
        posts = [{"id": "a"}, {"id": "b"}]
        seen = {engine.build_dedup_key(1, "a"): 100.0}
        unseen = engine.filter_unseen(posts, channel_id=1, seen_store=seen)
        assert [p["id"] for p in unseen] == ["b"]

    def test_filter_unseen_same_post_different_channel_is_not_filtered(self):
        posts = [{"id": "a"}]
        seen = {engine.build_dedup_key(1, "a"): 100.0}  # seen only for channel 1
        unseen = engine.filter_unseen(posts, channel_id=2, seen_store=seen)
        assert [p["id"] for p in unseen] == ["a"]

    def test_prune_dedup_store_drops_entries_past_ttl(self):
        now = 1_000_000.0
        store = {
            "old": now - (8 * 86400),   # 8 days old, ttl=7 -> dropped
            "fresh": now - (1 * 86400),  # 1 day old -> kept
        }
        pruned = engine.prune_dedup_store(store, now, ttl_days=7)
        assert pruned == {"fresh": store["fresh"]}

    def test_prune_dedup_store_keeps_entries_at_the_edge(self):
        now = 1_000_000.0
        store = {"edge": now - 1}  # 1 second old, well within any real ttl
        pruned = engine.prune_dedup_store(store, now, ttl_days=7)
        assert "edge" in pruned


# -- keyword filtering -------------------------------------------------------

class TestKeywordFilter:
    def test_no_filters_passes_everything(self):
        post = {"title": "anything", "selftext": ""}
        assert engine.passes_keyword_filter(post, [], []) is True

    def test_block_keyword_rejects_regardless_of_case(self):
        post = {"title": "A Spoiler post", "selftext": ""}
        assert engine.passes_keyword_filter(post, [], ["spoiler"]) is False

    def test_require_keyword_must_match_at_least_one(self):
        post = {"title": "cute cat photo", "selftext": ""}
        assert engine.passes_keyword_filter(post, ["dog", "cat"], []) is True
        assert engine.passes_keyword_filter(post, ["dog", "fish"], []) is False

    def test_block_wins_over_require(self):
        """REGRESSION: a post matching a require keyword must still be rejected
        if it also matches a block keyword -- block takes priority."""
        post = {"title": "cute cat spoiler", "selftext": ""}
        assert engine.passes_keyword_filter(post, ["cat"], ["spoiler"]) is False

    def test_checks_selftext_too(self):
        post = {"title": "no match here", "selftext": "but banned_word is in the body"}
        assert engine.passes_keyword_filter(post, [], ["banned_word"]) is False

    def test_handles_missing_selftext_key(self):
        post = {"title": "title only"}
        assert engine.passes_keyword_filter(post, [], ["nope"]) is True


# -- media extraction -------------------------------------------------------

class TestExtractMediaItems:
    def test_direct_image_url(self):
        post = {"url": "https://i.redd.it/abc123.jpg"}
        items = engine.extract_media_items(post)
        assert len(items) == 1
        assert items[0].kind == constants.MEDIA_KIND_IMAGE
        assert items[0].is_link_only is False
        assert items[0].url == post["url"]

    def test_text_post_yields_no_media(self):
        post = {"url": "https://www.reddit.com/r/feet/comments/abc/some_title/", "selftext": "just text"}
        assert engine.extract_media_items(post) == []

    def test_gallery_expands_to_one_item_per_image(self):
        post = {
            "is_gallery": True,
            "gallery_data": {"items": [{"media_id": "m1"}, {"media_id": "m2"}]},
            "media_metadata": {
                "m1": {"s": {"u": "https://preview.redd.it/m1.jpg?width=100&amp;s=abc"}},
                "m2": {"s": {"u": "https://preview.redd.it/m2.jpg"}},
            },
        }
        items = engine.extract_media_items(post)
        assert len(items) == 2
        assert all(i.kind == constants.MEDIA_KIND_GALLERY_IMAGE for i in items)
        # &amp; must be unescaped back to & in the URL
        assert items[0].url == "https://preview.redd.it/m1.jpg?width=100&s=abc"

    def test_gallery_skips_entries_missing_metadata(self):
        post = {
            "is_gallery": True,
            "gallery_data": {"items": [{"media_id": "missing"}]},
            "media_metadata": {},
        }
        assert engine.extract_media_items(post) == []

    def test_video_post_is_a_link_only_item(self):
        post = {"is_video": True, "permalink": "/r/feet/comments/abc/title/"}
        items = engine.extract_media_items(post)
        assert len(items) == 1
        assert items[0].kind == constants.MEDIA_KIND_VIDEO_LINK
        assert items[0].is_link_only is True
        assert items[0].url == "https://www.reddit.com/r/feet/comments/abc/title/"

    def test_redgifs_post_is_a_link_only_item(self):
        post = {"url": "https://redgifs.com/watch/somegif"}
        items = engine.extract_media_items(post)
        assert len(items) == 1
        assert items[0].kind == constants.MEDIA_KIND_REDGIFS_LINK
        assert items[0].is_link_only is True

    def test_gallery_takes_priority_over_other_fields(self):
        """A gallery post with a stray top-level url shouldn't double-post."""
        post = {
            "is_gallery": True,
            "gallery_data": {"items": [{"media_id": "m1"}]},
            "media_metadata": {"m1": {"s": {"u": "https://preview.redd.it/m1.jpg"}}},
            "url": "https://www.reddit.com/gallery/abc",
        }
        items = engine.extract_media_items(post)
        assert len(items) == 1
        assert items[0].kind == constants.MEDIA_KIND_GALLERY_IMAGE


# -- settings validation -------------------------------------------------------

class TestClamping:
    def test_poll_interval_floor(self):
        assert engine.clamp_poll_interval(5) == constants.MIN_POLL_INTERVAL_SECONDS

    def test_poll_interval_above_floor_unchanged(self):
        assert engine.clamp_poll_interval(300) == 300

    def test_stagger_clamped_to_range(self):
        assert engine.clamp_stagger(-5) == constants.MIN_STAGGER_SECONDS
        assert engine.clamp_stagger(999) == constants.MAX_STAGGER_SECONDS
        assert engine.clamp_stagger(2.5) == 2.5


class TestPartitionMediaItems:
    def test_splits_image_items_from_link_items(self):
        image = MediaItem(kind=constants.MEDIA_KIND_IMAGE, url="https://a/1.jpg")
        gallery = MediaItem(kind=constants.MEDIA_KIND_GALLERY_IMAGE, url="https://a/2.jpg")
        video = MediaItem(kind=constants.MEDIA_KIND_VIDEO_LINK, url="https://reddit.com/x", is_link_only=True)
        image_items, link_items = engine.partition_media_items([image, gallery, video])
        assert image_items == [image, gallery]
        assert link_items == [video]

    def test_empty_list(self):
        assert engine.partition_media_items([]) == ([], [])


class TestChunkItems:
    def test_splits_into_chunks_of_requested_size(self):
        assert engine.chunk_items([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_single_chunk_when_under_size(self):
        assert engine.chunk_items([1, 2], 10) == [[1, 2]]

    def test_empty_list_yields_no_chunks(self):
        assert engine.chunk_items([], 10) == []

    def test_rejects_non_positive_size(self):
        with pytest.raises(ValueError):
            engine.chunk_items([1], 0)


class TestSortPostsOldestFirst:
    def test_sorts_ascending_by_created_utc(self):
        posts = [{"id": "new", "created_utc": 300}, {"id": "old", "created_utc": 100}]
        sorted_posts = engine.sort_posts_oldest_first(posts)
        assert [p["id"] for p in sorted_posts] == ["old", "new"]
