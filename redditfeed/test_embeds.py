from redditfeed import embeds
from redditfeed.models import MediaItem, SubredditMapping


class TestBuildLinkMessage:
    def test_returns_bare_url_no_text(self):
        item = MediaItem(kind="image", url="https://i.redd.it/x.jpg")
        assert embeds.build_link_message({"title": "ignored"}, item) == "https://i.redd.it/x.jpg"


class TestBuildGalleryEmbeds:
    def test_one_embed_per_image(self):
        items = [
            MediaItem(kind="gallery_image", url="https://preview.redd.it/1.jpg"),
            MediaItem(kind="gallery_image", url="https://preview.redd.it/2.jpg"),
        ]
        result = embeds.build_gallery_embeds({"id": "abc"}, items)
        assert len(result) == 2
        assert result[0].image_url == "https://preview.redd.it/1.jpg"
        assert result[1].image_url == "https://preview.redd.it/2.jpg"

    def test_all_embeds_share_the_same_grouping_url(self):
        """REGRESSION: Discord only tiles embeds into one gallery message when
        they share an identical embed.url -- if each embed got a different url
        (e.g. derived per-image), they'd render as separate stacked embeds
        instead of a grouped gallery."""
        items = [
            MediaItem(kind="gallery_image", url="https://preview.redd.it/1.jpg"),
            MediaItem(kind="gallery_image", url="https://preview.redd.it/2.jpg"),
            MediaItem(kind="gallery_image", url="https://preview.redd.it/3.jpg"),
        ]
        result = embeds.build_gallery_embeds({"id": "abc"}, items)
        urls = {e.url for e in result}
        assert len(urls) == 1

    def test_no_title_or_description_set(self):
        """The shared grouping url must never be visibly rendered -- that only
        happens if a title is also set (title becomes the clickable link)."""
        items = [MediaItem(kind="gallery_image", url="https://preview.redd.it/1.jpg")]
        result = embeds.build_gallery_embeds({"id": "abc"}, items)
        assert result[0].title is None
        assert result[0].description is None

    def test_different_posts_get_different_grouping_urls(self):
        item = MediaItem(kind="gallery_image", url="https://preview.redd.it/1.jpg")
        a = embeds.build_gallery_embeds({"id": "post_a"}, [item])
        b = embeds.build_gallery_embeds({"id": "post_b"}, [item])
        assert a[0].url != b[0].url


# -- .redditfeed map overview ---------------------------------------------------------------------

def _m(name, channels, **kw):
    return SubredditMapping(subreddit=name, channel_ids=channels, **kw)


class TestMappingOverview:
    CAT = {10: "2d", 11: "3d", 12: "irl"}

    def test_lists_unmapped_channels_in_the_category(self):
        maps = [_m("hentai", [10]), _m("hentai_gif", [10]), _m("feet", [12])]
        text = "\n".join(embeds.build_mapping_overview(maps, self.CAT, "Feeds", {10, 11, 12}))
        assert "<#10> ← r/hentai, r/hentai_gif" in text
        assert "<#12> ← r/feet" in text
        unmapped = text.split("NOT mapped**")[1]
        assert "<#11>" in unmapped and "<#10>" not in unmapped.split("**Mapped, outside")[0]
        assert "3 subreddit(s) -> 2 channel(s)" in text

    def test_all_mapped_says_so(self):
        text = "\n".join(embeds.build_mapping_overview([_m("a", [10]), _m("b", [11]), _m("c", [12])], self.CAT, "Feeds", {10, 11, 12}))
        assert "every channel in the category is mapped" in text

    def test_auto_and_paused_are_called_out_but_manual_is_plain(self):
        maps = [_m("a", [10], approval="auto"), _m("b", [10], paused=True), _m("c", [10])]
        text = "\n".join(embeds.build_mapping_overview(maps, self.CAT, "Feeds", {10, 11, 12}))
        assert "r/a (auto)" in text and "r/b (paused)" in text and "r/c," not in text and text.rstrip().find("r/c") > 0

    def test_one_subreddit_in_two_channels_appears_under_both(self):
        text = "\n".join(embeds.build_mapping_overview([_m("a", [10, 11])], self.CAT, "Feeds", {10, 11, 12}))
        assert "<#10> ← r/a" in text and "<#11> ← r/a" in text

    def test_outside_deleted_and_channelless(self):
        maps = [_m("a", [99]), _m("b", [555]), _m("c", [])]
        text = "\n".join(embeds.build_mapping_overview(maps, self.CAT, "Feeds", {10, 11, 12, 99}))
        assert "Mapped, outside Feeds" in text and "<#99> ← r/a" in text
        assert "no longer exists" in text and "555: r/b" in text
        assert "Subreddits with no channel" in text and "r/c" in text.split("no channel")[1]

    def test_without_a_category_it_still_lists_mappings_and_hints(self):
        text = "\n".join(embeds.build_mapping_overview([_m("a", [10])], {}, None, {10}))
        assert "<#10> ← r/a" in text and ".redditfeed map <category>" in text and "NOT mapped" not in text


class TestPaginateLines:
    def test_splits_between_lines_under_the_limit(self):
        pages = embeds.paginate_lines(["a" * 60, "b" * 60, "c" * 60], limit=130)
        assert pages == ["a" * 60 + "\n" + "b" * 60, "c" * 60]

    def test_overlong_single_line_is_hard_cut(self):
        pages = embeds.paginate_lines(["x" * 250], limit=100)
        assert [len(p) for p in pages] == [100, 100, 50]

    def test_empty(self):
        assert embeds.paginate_lines([]) == []
