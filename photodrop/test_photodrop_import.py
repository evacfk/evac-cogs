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


def test_collage_rendering_never_runs_directly_on_the_event_loop():
    """REGRESSION: collage.save_* (Pillow open/resize/PNG-optimize, once per member in
    the weekly poll) used to be called inline from async commands, stalling the whole
    bot. Every call must be handed to asyncio.to_thread instead of being invoked directly."""
    import ast
    import inspect

    from photodrop import photodrop

    tree = ast.parse(inspect.getsource(photodrop))
    direct = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "collage"
        and node.func.attr.startswith("save_")
    ]
    assert direct == [], f"collage.save_* called directly on the loop at lines {direct}"

    offloaded = [
        a.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "to_thread"
        for a in node.args[:1]
        if isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "collage"
    ]
    assert len(offloaded) == 4  # month, per-photo, rated gallery, weekly poll


def test_pp_version_probe_exists_and_matches_the_constant():
    from photodrop import photodrop
    from photodrop.constants import COG_VERSION

    assert hasattr(photodrop.PhotoDrop, "pp_version")
    assert COG_VERSION == "1.1.0"
