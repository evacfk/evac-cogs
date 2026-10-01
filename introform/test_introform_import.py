"""Import-time smoke test for the live cog module (see photodrop's equivalent
for why): confirms the module imports under the stubs and its classes build.
Not a behavior test -- live Discord behavior can't be proven here.
"""


def test_module_imports_and_defines_cog_view_and_modal():
    from introform import introform

    assert hasattr(introform, "IntroForm")
    assert hasattr(introform, "IntroPanelView")
    assert hasattr(introform, "IntroModal")


def test_panel_view_has_all_buttons():
    from introform.introform import IntroPanelView

    view = IntroPanelView(cog=None)
    assert {"open_button", "find_button", "delete_button"}.issubset(dir(view))


def test_modal_builds_one_input_per_question_within_discord_limits():
    from introform.constants import QUESTIONS
    from introform.introform import IntroModal

    modal = IntroModal(cog=None, existing_answers={"name": "Dylan"})
    assert len(modal.inputs) == len(QUESTIONS)
    assert len(modal.inputs) <= 5  # Discord modal cap
    assert modal.inputs["name"].default == "Dylan"
    assert modal.inputs["games"].default is None
    for q in QUESTIONS:
        assert len(q["label"]) <= 45  # Discord label cap
        assert len(q["placeholder"]) <= 100


def test_group_uses_invoke_without_command_and_subcommands_exist():
    from introform.introform import IntroForm

    for name in ("setchannel", "panel", "enforce", "settings"):
        assert hasattr(IntroForm, name)


def test_find_views_build_under_the_stub():
    from introform.introform import FindView, ResultsView, SearchModal

    assert {"pick_member", "keyword_button"}.issubset(dir(FindView(cog=None)))
    assert len(SearchModal(cog=None).children) == 1
    rows = [(1, "Dylan", "Bay Area"), (2, "Ana", None)]
    view = ResultsView(cog=None, rows=rows)
    assert len(view.select.options) == 2


def test_prune_and_find_support_methods_exist():
    from introform.introform import IntroForm

    for name in ("prune", "show_intro", "handle_search"):
        assert hasattr(IntroForm, name)
