from redditfeed import embeds
from redditfeed.models import MediaItem


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
