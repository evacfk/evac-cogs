"""Import-time smoke test for the live cog module.

Every other test file exercises models/storage/collage/embeds directly and
never imports photodrop.py itself, so a module-level error in it (a missing
stub attribute, a bad decorator, a typo in an import) could ship unnoticed
even with every other test green -- this is exactly how the missing
`discord.Color.green()` stub attribute slipped through while building the
rating feature. This test's only job is to import the module and confirm
the classes it defines actually come into existence; it is not a behavior
test (the stub in conftest.py doesn't implement real Discord machinery).
"""


def test_module_imports_and_defines_the_cog_and_rating_view():
    from photodrop import photodrop

    assert hasattr(photodrop, "PhotoDrop")
    assert hasattr(photodrop, "_RatingView")


def test_rating_view_has_all_four_rating_buttons():
    from photodrop.photodrop import _RatingView

    view = _RatingView(cog=None)
    button_names = {"goat_button", "good_button", "mid_button", "bad_button"}
    assert button_names.issubset(dir(view))
