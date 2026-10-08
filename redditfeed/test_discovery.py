import pytest

from redditfeed import discovery
from redditfeed.discovery import FilterStats


def raw(name, subs=1000, desc="", over18=True, quarantine=False, sr_type="restricted"):
    return {
        "display_name": name,
        "subscribers": subs,
        "public_description": desc,
        "over18": over18,
        "quarantine": quarantine,
        "subreddit_type": sr_type,
        "created_utc": 1498074311,
    }


# -- candidate_from_result ---------------------------------------------------------

class TestCandidateFromResult:
    def test_parses_a_normal_result(self):
        c = discovery.candidate_from_result(raw("FeetInYourFace", 433657, "foot stuff"))
        assert (c.name, c.display_name, c.subscribers, c.description) == (
            "feetinyourface", "FeetInYourFace", 433657, "foot stuff",
        )

    @pytest.mark.parametrize(
        "item",
        [
            raw("NotNsfw", over18=False),
            raw("Quarantined", quarantine=True),
            raw("Hidden", sr_type="private"),
            {"subscribers": 5, "over18": True},          # no name
            {"display_name": "", "over18": True},
        ],
    )
    def test_unusable_results_are_none(self, item):
        assert discovery.candidate_from_result(item) is None

    def test_missing_or_bad_subscriber_count_becomes_zero(self):
        item = raw("Odd")
        item["subscribers"] = None
        assert discovery.candidate_from_result(item).subscribers == 0
        item["subscribers"] = "lots"
        assert discovery.candidate_from_result(item).subscribers == 0


# -- safety screen -------------------------------------------------------------------

class TestSafetyScreen:
    @pytest.mark.parametrize("name", ["TeenFeet", "young_feet", "School-Girl-Feet", "BabyFeetLove"])
    def test_blocked_names(self, name):
        assert discovery.name_is_blocked(name) is True

    @pytest.mark.parametrize("name", ["FeetInYourFace", "Feet_Queens", "rule34feet", "animefeets", "FootFetish"])
    def test_ordinary_names_pass(self, name):
        assert discovery.name_is_blocked(name) is False

    def test_name_screen_is_deliberately_overbroad(self):
        """Documented trade-off: substring matching on names also hides things
        like 'hololive' (contains 'loli'). A hidden suggestion is cheap
        (`.redditfeed add` still works); a missed one is not."""
        assert discovery.name_is_blocked("hololive") is True

    @pytest.mark.parametrize(
        "text", ["young models only", "Barely Legal", "TEENS", "she is a kid", "school girl outfits"]
    )
    def test_blocked_text(self, text):
        assert discovery.text_is_blocked(text) is True

    @pytest.mark.parametrize(
        "text", ["", "A collection of foot pics", "nineteen eighty four", "kidney stones", "Youngstown Ohio"]
    )
    def test_prose_uses_whole_words_so_ordinary_text_passes(self, text):
        assert discovery.text_is_blocked(text) is False


# -- filter_candidates ---------------------------------------------------------------

class TestFilterCandidates:
    def _run(self, **kwargs):
        results = [
            raw("Feet_Queens", 272483),
            raw("FeetInYourFace", 433657),
            raw("TeenFeet", 90000),                                   # name screen
            raw("FootDesc", 80000, desc="young models"),              # description screen
            raw("AlreadyMapped", 70000),
            raw("DeniedOne", 60000),
            raw("SfwSub", 50000, over18=False),                       # unusable
            raw("Quarantined", 40000, quarantine=True),               # unusable
            raw("feetinyourface", 433657),                            # duplicate from a 2nd prefix
        ]
        return discovery.filter_candidates(
            results, mapped=["alreadymapped"], denied=["DeniedOne"], **kwargs
        )

    def test_keeps_only_clean_new_candidates_biggest_first(self):
        kept, _ = self._run()
        assert [c.name for c in kept] == ["feetinyourface", "feet_queens"]

    def test_counts_what_was_hidden_and_why(self):
        _, stats = self._run()
        assert stats == FilterStats(
            found=6, already_mapped=1, denied=1, safety_filtered=2, not_usable=2,
        )

    def test_limit_caps_the_suggestions(self):
        kept, _ = self._run(limit=1)
        assert [c.name for c in kept] == ["feetinyourface"]


# -- pick_preview ------------------------------------------------------------------------

def post(pid, score, url=None, title="nice", **extra):
    base = {"id": pid, "score": score, "title": title, "permalink": f"/r/x/comments/{pid}/t/"}
    if url:
        base["url"] = url
    base.update(extra)
    return base


def gallery(pid, score, n):
    ids = [f"m{i}" for i in range(n)]
    return post(
        pid, score,
        is_gallery=True,
        gallery_data={"items": [{"media_id": m} for m in ids]},
        media_metadata={m: {"s": {"u": f"https://preview.redd.it/{pid}{m}.jpg?a=1&amp;b=2"}} for m in ids},
    )


class TestPickPreview:
    def _posts(self):
        return [
            post("a", 10, "https://i.redd.it/a.jpg"),
            gallery("b", 50, 3),
            post("c", 30, is_video=True),
            post("d", 100, "https://i.redd.it/d.jpg", title="cute teen outfit"),
            post("e", 1, "https://i.redd.it/e.jpg"),
            post("f", 7),                                    # text only
        ]

    def test_best_scoring_image_posts_one_tile_per_post(self):
        result = discovery.pick_preview(self._posts())
        assert [i.url for i in result.images] == [
            "https://preview.redd.it/bm0.jpg?a=1&b=2",       # gallery contributes ONE tile, entity-decoded
            "https://i.redd.it/a.jpg",
            "https://i.redd.it/e.jpg",
        ]

    def test_flagged_titles_are_excluded_and_counted(self):
        result = discovery.pick_preview(self._posts())
        assert result.flagged_titles == 1
        assert all("d.jpg" not in i.url for i in result.images)

    def test_video_only_posts_are_counted_not_tiled(self):
        assert discovery.pick_preview(self._posts()).video_or_link_posts == 1

    def test_links_follow_the_previewed_posts(self):
        result = discovery.pick_preview(self._posts())
        assert result.post_links == [
            "https://www.reddit.com/r/x/comments/b/t/",
            "https://www.reddit.com/r/x/comments/a/t/",
            "https://www.reddit.com/r/x/comments/e/t/",
        ]

    def test_caps_apply(self):
        result = discovery.pick_preview(self._posts(), max_images=2, max_links=1)
        assert len(result.images) == 2
        assert len(result.post_links) == 1

    def test_empty_input(self):
        result = discovery.pick_preview([])
        assert (result.images, result.post_links, result.posts_seen) == ([], [], 0)


# -- command text ---------------------------------------------------------------------------

class TestParsePrefixes:
    def test_normalizes_dedupes_and_drops_junk(self):
        text = "feet, foot,FEET,r/sole,a,bad name,thisiswaytoolongsubredditnamehere"
        assert discovery.parse_prefixes(text) == ["feet", "foot", "sole"]

    def test_caps_the_count(self):
        assert len(discovery.parse_prefixes("aa,bb,cc,dd,ee,ff,gg")) == 5

    def test_empty(self):
        assert discovery.parse_prefixes("") == []
        assert discovery.parse_prefixes(" , ,") == []


class TestFormatSummary:
    def test_lists_only_nonzero_hidden_categories(self):
        stats = FilterStats(found=6, already_mapped=1, denied=0, safety_filtered=2)
        text = discovery.format_summary(stats, 3, ["feet", "foot"], 5000, [])
        assert "**6**" in text and "showing **3**" in text
        assert "1 already mapped" in text and "2 hidden by the name/description safety screen" in text
        assert "denied earlier" not in text

    def test_nothing_to_show_says_so(self):
        text = discovery.format_summary(FilterStats(), 0, ["feet"], 5000, [])
        assert "Nothing new to suggest" in text

    def test_search_failures_are_reported(self):
        text = discovery.format_summary(FilterStats(), 0, ["feet"], 5000, ["`feet`: HTTP 422: Timeout"])
        assert "Search failed for `feet`: HTTP 422: Timeout" in text
