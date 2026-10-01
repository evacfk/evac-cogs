from introform import engine
from introform.constants import QUESTIONS

ZWSP = engine.ZWSP


def good_answers(**overrides):
    answers = {
        "name": "Dylan",
        "age_gender": "30, he/him",
        "location": "Bay Area",
        "games": "Magic, Forza",
        "extra": "Truck driver",
    }
    answers.update(overrides)
    return answers


def test_clean_text_defuses_mass_pings():
    out = engine.clean_text("hi @everyone and @here", 100)
    assert "@everyone" not in out and "@here" not in out
    assert f"@{ZWSP}everyone" in out


def test_clean_text_collapses_blank_lines_and_truncates():
    assert engine.clean_text("a\n\n\n\n\nb", 100) == "a\n\nb"
    assert engine.clean_text("x" * 50, 10) == "x" * 10
    assert engine.clean_text(None, 10) == ""


def test_normalize_drops_unknown_keys_and_fills_missing():
    out = engine.normalize_answers({"name": " Bo ", "bogus": "x"})
    assert out["name"] == "Bo"
    assert "bogus" not in out
    assert set(out) == {q["key"] for q in QUESTIONS}
    assert out["games"] == ""


def test_validate_accepts_complete_answers():
    assert engine.validate_answers(good_answers()) == []


def test_validate_flags_each_missing_required_field():
    problems = engine.validate_answers(good_answers(name="", games=""))
    assert len(problems) == 2


def test_validate_allows_missing_optional_fields():
    assert engine.validate_answers(good_answers(location="", extra="")) == []


def test_validate_rejects_invite_links_once():
    problems = engine.validate_answers(
        good_answers(extra="join discord.gg/abc123", games="https://discord.com/invite/xyz")
    )
    assert len(problems) == 1
    assert "invite" in problems[0].lower()


def test_embed_fields_skip_empty_and_title_field_in_form_order():
    fields = engine.embed_fields(good_answers(location=""))
    names = [f[0] for f in fields]
    assert names == ["Age & Gender", "Games I Play", "About Me"]


def test_prefill_roundtrip_strips_zwsp_and_respects_max_length():
    cleaned = engine.clean_text("@everyone", 100)
    assert engine.prefill_value({"name": cleaned}, "name", 40) == "@everyone"
    assert engine.prefill_value({"name": "x" * 100}, "name", 40) == "x" * 40
    assert engine.prefill_value({}, "name", 40) is None
