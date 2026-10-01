import types

from introform import embeds


def fake_member():
    return types.SimpleNamespace(
        id=123,
        mention="<@123>",
        display_name="RacingKing96",
        display_avatar=types.SimpleNamespace(url="http://example/a.png"),
    )


def test_intro_embed_uses_name_as_title_and_skips_empty_fields():
    answers = {"name": "Dylan", "age_gender": "30", "location": "", "games": "Magic", "extra": ""}
    embed = embeds.build_intro_embed(fake_member(), answers)
    assert embed.title == "Dylan"
    assert [f.name for f in embed.fields] == ["Age & Gender", "Games I Play"]
    assert "<@123>" in embed.description


def test_intro_embed_falls_back_to_display_name():
    embed = embeds.build_intro_embed(fake_member(), {"name": "", "games": "x"})
    assert embed.title == "RacingKing96"


def test_panel_embed_mentions_removal_only_when_enforcing():
    assert "removed" in embeds.build_panel_embed(True).description
    assert "removed" not in embeds.build_panel_embed(False).description


def test_panel_embed_mentions_find_button():
    assert "Find an Intro" in embeds.build_panel_embed(False).description
