from photodrop import embeds
from photodrop.constants import STATUS_FULL, STATUS_TARDY


class TestShiftReportEmbed:
    def test_full_quota_shows_streak_and_no_tardy_mark(self):
        embed = embeds.shift_report_embed("evac", STATUS_FULL, 3, 3, 14)
        assert "3/3" in embed.title
        assert "Tardy" not in embed.title
        assert any(f.name == "Streak" and "14" in f.value for f in embed.fields)

    def test_partial_quota_shows_tardy(self):
        embed = embeds.shift_report_embed("evac", STATUS_TARDY, 2, 3, 0)
        assert "2/3" in embed.title
        assert "Tardy" in embed.title


class TestStatusEmbed:
    def test_shows_streak_strikes_and_active_role(self):
        embed = embeds.status_embed("evac", 14, 1, 3, 30, True)
        values = [f.value for f in embed.fields]
        assert any("14" in v for v in values)
        assert any("1/3" in v for v in values)
        assert any("Active" in v for v in values)

    def test_shows_removed_role(self):
        embed = embeds.status_embed("evac", 0, 3, 3, 30, False)
        values = [f.value for f in embed.fields]
        assert any("Removed" in v for v in values)


class TestCalendarEmbed:
    def test_skips_days_with_no_entry(self):
        entries = [(1, None), (2, "full"), (3, None)]
        embed = embeds.calendar_embed("evac", "September 2026", entries)
        assert embed.description.count("\n") == 0  # only one logged day -> no newline

    def test_empty_month_shows_placeholder(self):
        embed = embeds.calendar_embed("evac", "September 2026", [(1, None)])
        assert "No entries" in embed.description


class TestRatingPromptEmbed:
    def test_single_photo_has_no_count_suffix(self):
        embed = embeds.rating_prompt_embed("evac", "2026-09-21", 0, 1)
        assert "#1" not in embed.title
        assert "evac" in embed.title
        assert "2026-09-21" in embed.title

    def test_multi_photo_shows_index_and_count(self):
        embed = embeds.rating_prompt_embed("evac", "2026-09-21", 1, 3)
        assert "#2/3" in embed.title


class TestRatedEmbed:
    def test_title_includes_rating_label_and_uses_rating_color(self):
        embed = embeds.rated_embed("evac", "2026-09-21", 0, 1, "goat")
        assert "GOAT" in embed.title
        assert embed.color == embeds.RATING_COLORS["goat"]

    def test_different_ratings_use_different_colors(self):
        goat = embeds.rated_embed("evac", "2026-09-21", 0, 1, "goat")
        bad = embeds.rated_embed("evac", "2026-09-21", 0, 1, "bad")
        assert goat.color != bad.color
