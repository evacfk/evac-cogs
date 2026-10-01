from introform import engine
from introform.constants import QUESTIONS

ZWSP = engine.ZWSP


def good_answers(**overrides):
    answers = {
        "name": "Dylan",
        "age_gender": "30, he/him",
        "location": "Bay Area",
        "extra": "Truck driver. I play Magic and Forza.",
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
    assert out["extra"] == ""
    assert "games" not in out


def test_validate_accepts_complete_answers():
    assert engine.validate_answers(good_answers()) == []


def test_validate_flags_each_missing_required_field():
    problems = engine.validate_answers(good_answers(name="", extra=""))
    assert len(problems) == 2


def test_validate_allows_missing_optional_fields():
    assert engine.validate_answers(good_answers(location="")) == []


def test_validate_rejects_invite_links_once():
    problems = engine.validate_answers(
        good_answers(extra="join discord.gg/abc123 or https://discord.com/invite/xyz")
    )
    assert len(problems) == 1
    assert "invite" in problems[0].lower()


def test_description_orders_byline_details_then_about():
    text = engine.build_description("<@1>", good_answers(location=""))
    assert text.startswith("Intro by <@1>")
    assert "**Age & Gender:** 30, he/him" in text
    assert "**Location:**" not in text  # empty answers are skipped
    assert text.index("Age & Gender") < text.index("**About Me**")
    assert text.endswith("Truck driver. I play Magic and Forza.")


def test_description_fits_embed_limit_at_max_length():
    answers = good_answers(name="N" * 40, age_gender="A" * 40, location="L" * 60, extra="x" * 2000)
    assert len(engine.build_description("<@123456789012345678>", answers)) < 4096


def test_migrate_folds_legacy_games_into_about_me_once():
    old = {"name": "Dylan", "games": "Magic", "extra": "Truck driver"}
    once = engine.migrate_answers(old)
    assert "games" not in once
    assert once["extra"] == "Truck driver\n\nGames I play: Magic"
    assert engine.migrate_answers(once) == once  # idempotent
    assert engine.migrate_answers({"games": "Chess"})["extra"] == "Games I play: Chess"
    assert engine.migrate_answers({"name": "x"}) == {"name": "x"}


def test_prefill_roundtrip_strips_zwsp_and_respects_max_length():
    cleaned = engine.clean_text("@everyone", 100)
    assert engine.prefill_value({"name": cleaned}, "name", 40) == "@everyone"
    assert engine.prefill_value({"name": "x" * 100}, "name", 40) == "x" * 40
    assert engine.prefill_value({}, "name", 40) is None


def intros_fixture():
    return {
        "1": {"message_id": 10, "answers": good_answers(name="Dylan", extra="I play Magic, Forza", location="Bay Area")},
        "2": {"message_id": 11, "answers": good_answers(name="Magic Mike", extra="Chess", location="Texas")},
        "3": {"message_id": 12, "answers": good_answers(name="Ana", extra="Valorant", location="Romania")},
        "x": {"message_id": 13, "answers": good_answers(name="Bad key")},
    }


def test_search_matches_any_field_and_ranks_name_hits_first():
    matches, total = engine.search_intros(intros_fixture(), "magic")
    assert total == 2
    # "Magic Mike" has it in his name, Dylan only in About Me
    assert [uid for uid, _ in matches] == [2, 1]


def test_search_requires_every_word():
    matches, total = engine.search_intros(intros_fixture(), "magic texas")
    assert [uid for uid, _ in matches] == [2]
    assert total == 1


def test_search_skips_members_who_left():
    matches, total = engine.search_intros(intros_fixture(), "magic", is_member=lambda uid: uid != 2)
    assert [uid for uid, _ in matches] == [1]
    assert total == 1


def test_search_limit_caps_results_but_reports_total():
    intros = {str(i): {"message_id": i, "answers": good_answers(name=f"Player {i:02d}", extra="Magic")} for i in range(40)}
    matches, total = engine.search_intros(intros, "magic", limit=25)
    assert len(matches) == 25 and total == 40


def test_search_blank_query_and_bad_keys():
    assert engine.search_intros(intros_fixture(), "   ") == ([], 0)
    matches, _ = engine.search_intros(intros_fixture(), "bad key")
    assert matches == []  # non-numeric user id keys are ignored


def test_option_label_and_description_fit_discord_limits():
    a = good_answers(name="N" * 40, location="L" * 60, extra="g\n" * 500)
    assert len(engine.option_label(a)) <= 100
    desc = engine.option_description(a)
    assert desc is not None and len(desc) <= 100 and "\n" not in desc
    assert engine.option_description({"location": "", "extra": ""}) is None
    assert engine.option_label({"name": ""}) == "(no name)"


def test_search_still_finds_legacy_games_answers():
    intros = {"1": {"message_id": 1, "answers": {"name": "Old", "games": "Valorant", "extra": ""}}}
    matches, total = engine.search_intros(intros, "valorant")
    assert total == 1 and matches[0][0] == 1
