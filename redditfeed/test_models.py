from redditfeed.models import MediaItem, SubredditMapping


class TestSubredditMappingRoundTrip:
    def test_to_dict_from_dict_round_trip(self):
        mapping = SubredditMapping(
            subreddit="feet",
            channel_ids=[111, 222],
            require_keywords=["sunset"],
            block_keywords=["spoiler"],
            paused=True,
            last_poll_ts=1000.0,
            last_post_found_ts=999.0,
            last_post_id="abc",
            last_error="boom",
        )
        restored = SubredditMapping.from_dict(mapping.to_dict())
        assert restored == mapping

    def test_from_dict_fills_defaults_for_missing_optional_fields(self):
        restored = SubredditMapping.from_dict({"subreddit": "feetish"})
        assert restored.channel_ids == []
        assert restored.require_keywords == []
        assert restored.block_keywords == []
        assert restored.paused is False
        assert restored.last_poll_ts is None
        assert restored.last_post_id is None

    def test_channel_ids_list_is_independent_per_instance(self):
        """REGRESSION: a mutable default (list) shared across instances would
        leak channel_ids between unrelated mappings."""
        a = SubredditMapping(subreddit="a")
        b = SubredditMapping(subreddit="b")
        a.channel_ids.append(1)
        assert b.channel_ids == []


class TestMediaItemRoundTrip:
    def test_to_dict_from_dict_round_trip(self):
        item = MediaItem(kind="image", url="https://example.com/x.jpg", is_link_only=False)
        restored = MediaItem.from_dict(item.to_dict())
        assert restored == item

    def test_from_dict_defaults_is_link_only_false(self):
        restored = MediaItem.from_dict({"kind": "image", "url": "https://example.com/x.jpg"})
        assert restored.is_link_only is False
